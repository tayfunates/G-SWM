# -*- coding: utf-8 -*-

import os
import os.path as osp
import json
import re
from enum import IntEnum
from copy import deepcopy

import torch
import torch.utils.data as data
from torchvision.io import read_video, read_video_timestamps
from torchvision.transforms import Lambda, ToTensor, Resize, Normalize
import numpy as np
import cv2
from tqdm import tqdm


UNK = "<UNK>"
PAD = "<PAD>"
PUNCTUATION_REGEX = re.compile(r"([a-zA-Z0-9]+)(\W)")


__all__ = (
    'SimulationInput',
    'IntuitivePhysicsDataset',
    'train_collate_fn',
    'inference_collate_fn',
)


def tokenize_sentence(sentence):
    return PUNCTUATION_REGEX.sub(r"\g<1> \g<2>", sentence.lower()).split()


def rearrange_dimensions(frames):
    # make channel dimension 1st dimension
    new_frames = frames.unsqueeze(0).transpose(0, -1).squeeze(-1)

    # remove depth in case of single frame
    C, D, H, W = new_frames.shape
    new_frames = new_frames.squeeze() if D == 1 else new_frames

    return new_frames


class SimulationInput(IntEnum):
    NO_FRAMES = 0
    FIRST_FRAME = 1
    LAST_FRAME = 2
    FIRST_AND_LAST_FRAMES = 3
    VIDEO = 4

    def from_string(obj):
        if isinstance(obj, int) or isinstance(obj, SimulationInput):
            return SimulationInput(obj)
        obj = "SimulationInput.{}".format(obj)
        symbol = eval(obj)
        return SimulationInput(symbol)


class Vocab(object):
    def __init__(self, instances, min_occur=0):
        self.min_occur = min_occur
        self.build_count_dict(instances)
        self.build_dicts()

    def build_count_dict(self, instances):
        count_dict = dict()
        for instance in instances:
            tokens = tokenize_sentence(str(instance))
            for token in tokens:
                count_dict[token] = count_dict.get(token, 0) + 1
        self.count_dict = count_dict

    def build_dicts(self):
        self.w2i, self.i2w = {PAD: 0}, {0: PAD}
        tokens = [word
                  for (word, count) in self.count_dict.items()
                  if count >= self.min_occur]
        tokens.sort()
        for (i,token) in enumerate(tokens):
            self.w2i[token] = i+1
            self.i2w[i+1] = token

        # add UNK token, just in case
        num_tokens = len(self.w2i)
        self.w2i[UNK] = num_tokens
        self.i2w[num_tokens] = UNK

    def __getitem__(self, x):
        x = (x,) if isinstance(x, int) else x
        x = x.tolist() if isinstance(x, torch.Tensor) else x
        x = tokenize_sentence(x) if isinstance(x, str) else x

        if all(isinstance(xi, str) for xi in x):
            x2i = self.w2i
        elif all(isinstance(xi, int) for xi in x):
            x2i = self.i2w
        else:
            raise Exception("Not all elements are same type for Vocab input.")
        x = [x2i.get(xi, x2i.get(UNK)) for xi in x]
        return x

    def __len__(self):
        return len(self.w2i)


class IntuitivePhysicsDataset(data.Dataset):
    def __init__(
            self, path,
            split="train",
            transform=Resize(size=(64, 64)), #FIXME: Remove added for gswm,
            fps=0,
            cached=False,
            num_seconds=10,
            num_examples=None):
        self.datadir = osp.abspath(osp.expanduser(path))
        self.split = split
        self.transform = transform
        self.normalizer = None
        self.sim_input = SimulationInput.VIDEO # depends on model
        self.fps = fps
        self.num_seconds = num_seconds
        self.num_examples = num_examples
        self.cached = cached
        self.cache = dict()

        self.first_frames = 20 #G-SWM only. number of first frames to train

        self.read_jsonfile()
        self.build_vocabs()
        self.build_split()

        if num_examples is not None and num_examples > 0:
            self.indices = torch.randperm(len(self.questions))[:num_examples]

    def read_jsonfile(self):
        with open(osp.join(self.datadir, "dataset.json")) as f:
            self.json_data = json.load(f)

    def build_vocabs(self):
        simulations = filter(lambda x: x["split"] == "train", self.json_data)
        questions = []
        for sim in simulations:
            questions.extend(sim["questions"]["questions"])
        self.question_vocab = Vocab([x['question'] for x in questions])
        self.answer_vocab = Vocab([x['answer'] for x in questions])

    def build_split(self):
        self.questions = []
        if self.split != "all":
            simulations = filter(
                lambda x: x["split"] == self.split, self.json_data)
        else:
            simulations = self.json_data
        for sim in simulations:
            self.questions.extend(sim["questions"]["questions"])

    def build_cache(self):
        questions = self.questions
        if self.num_examples is not None and self.num_examples > 0:
            questions = [questions[i] for i in self.indices]
        items = {(q["video_index"], q["video_filename"])
                 for q in questions}
        items = [{"video_index": x[0], "video_filename": x[1]}
                 for x in items]
        for item in tqdm(items):
            if item["video_index"] in self.cache.keys(): continue
            self.cache[item['video_index']] = self.read_simulation(item)

    def adapt2model(self, model):
        self.sim_input = model.SIMULATION_INPUT
        try:
            self.normalizer = model.frame_encoder.normalizer
        except AttributeError:
            pass
        if self.cached: self.build_cache()

    def read_simulation(self, item):
        sim_input = str(self.sim_input).split(".")[1]
        sim_func = eval("self.read_{}".format(sim_input.lower()))
        return sim_func(item)

    def read_frame(self, item, frame="first"):
        path = item["video_filename"]
        path = path.replace("videos", frame + "_frames").replace("mpg", "png")
        path = osp.abspath(osp.join(self.datadir, "..", path))
        image = cv2.imread(path)
        image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        image = torch.tensor(image).permute(2, 0, 1) / 255.0
        return self.postprocess_simulation(image)

    def read_first_frame(self, item):
        return self.read_frame(item)

    def read_last_frame(self, item):
        return self.read_frame(item, frame="last")

    def read_first_and_last_frames(self, item):
        first_frame = self.read_first_frame(item)
        last_frame = self.read_last_frame(item)
        return (first_frame, last_frame)

    def read_video(self, item):
        filename = item["video_filename"]
        if self.fps > 0:
            filename = filename.replace("videos", f"downsampled/{self.fps}fps")
            filename = filename.replace(".mpg", ".mp4")
        video_path = osp.abspath(osp.join(self.datadir, "..", filename))
        video = read_video(video_path, pts_unit="sec")[0]
        video = rearrange_dimensions(video)
        return self.postprocess_simulation(video / 255.)

    def read_no_frames(self, item):
        return (torch.zeros(1),)

    def postprocess_simulation(self, simulation):
        processed = simulation

        # normalization
        if self.normalizer is not None:
            if len(processed.size())==4:
                C, T, W, H = processed.size()
                processed = self.normalizer(processed.reshape(T, C, W, H)).reshape(C, T, W, H)
            else:
                C, W, H = processed.size()
                processed = self.normalizer(processed)

        # transformation after normalization
        if self.transform is not None:
            processed = self.transform(processed)

        # make it appropriate for minibatching
        processed = processed.unsqueeze(0)
        return processed

    def __len__(self):
        if self.num_examples is not None: return self.num_examples
        return len(self.questions)

    def __getitem__(self, idx):
        if self.num_examples is not None and self.num_examples > 0:
            idx = self.indices[idx]
        item = self.questions[idx]
        if self.cached and item['video_index'] in self.cache.keys():
            simulation = self.cache[item['video_index']]
        elif not self.cached:
            simulation = self.read_simulation(item)
        else:
            print("read simulation op: split={}, video_index={}".format(
                self.split, item["video_index"]))
        if isinstance(simulation, torch.Tensor):
            simulation = (simulation,)
        question = self.question_vocab[item["question"]]
        answer = self.answer_vocab[tokenize_sentence(str(item["answer"]))]
        item_dict = {
            "simulation": simulation,
            "question": torch.tensor(question),
            "answer": torch.tensor(answer),
            "template": osp.splitext(item["template_filename"])[0],
            "video": item["video"],
            "video_index": item["video_index"],
            "question_index": item["question_index"],
        }

        #GSWM only - no text use
        ret = item_dict['simulation'][0].squeeze()
        shape = ret.shape
        ret = ret.reshape(shape[1], shape[0], shape[2], shape[3])[:self.first_frames, :, :, :]

        return ret, torch.zeros(0)


def base_collate_fn(batch):
    # question batching
    batchsize, longest = len(batch), len(batch[0]["question"])
    questions = torch.zeros((longest, batchsize), dtype=torch.long)
    for (i, instance) in enumerate(batch):
        question = instance["question"]
        questions[-len(question):, i] = question

    # answer batching
    answers = torch.cat([instance["answer"] for instance in batch])

    # simulation batching
    num_simulations = len(batch[0]["simulation"])
    helper = lambda i: torch.cat([x["simulation"][i] for x in batch], dim=0)
    simulations = torch.cat([helper(i) for i in range(num_simulations)], dim=0)

    return (simulations, questions, answers)


def train_collate_fn(unsorted_batch):
    unsorted_batch = [x for x in unsorted_batch if x is not None]
    batch = sorted(unsorted_batch,
                   key=lambda x: len(x["question"]),
                   reverse=True)
    simulations, questions, answers = base_collate_fn(batch)
    additional = {"kwargs": {}}
    inputs, outputs = (simulations, questions, additional), (answers,)
    return (inputs, outputs)


def inference_collate_fn(unsorted_batch):
    unsorted_batch = [x for x in unsorted_batch if x is not None]
    batch = sorted(unsorted_batch,
                   key=lambda x: len(x["question"]),
                   reverse=True)
    simulations, questions, answers = base_collate_fn(batch)
    additional = {
        "video_indexes": [x["video_index"] for x in batch],
        "question_indexes": [x["question_index"] for x in batch],
        "kwargs": {},
    }
    inputs, outputs = (simulations, questions, additional), (answers,)
    return (inputs, outputs)
