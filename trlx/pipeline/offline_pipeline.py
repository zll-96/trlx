import logging
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Tuple, Union

import torch
from torch.nn.utils.rnn import pad_sequence
from torch.utils.data import DataLoader
from transformers import (
    DataCollatorWithPadding,
    PreTrainedTokenizer,
    PreTrainedTokenizerFast,
)

from trlx.data.ilql_types import (
    ILQLBatch,
    ILQLElement,
    ILQLSeq2SeqBatch,
    ILQLSeq2SeqElement,
)
from trlx.pipeline import BasePipeline, BaseRolloutStore, BaseRolloutStreamStore, register_datapipeline


@dataclass
class DialogMessage:
    """
    Single message in a dialogue

    :param is_output: Whether the message is a model output or a prompt
    :type is_output: bool

    :param tokens: Tokenized message
    :type tokens: Tuple[int]
    """

    is_output: bool
    tokens: Tuple[int]


@dataclass
class PromptMessage:
    """
    Single message in a prompt

    :param tokens: Tokenized message
    :type tokens: Tuple[int]

    :param mask: Mask of tokens
    :type mask: Tuple[bool]
    """

    tokens: Tuple[int]
    mask: Tuple[bool]


def middle_truncate(tokenized: Union[Iterable[DialogMessage], Iterable[PromptMessage]], max_length: int,
                    truncation_side: str, start_char_token_id: int, end_char_token_id: int, sep_char_token_id: int):
    if isinstance(tokenized[0], DialogMessage):
        # TODO: only support one dialog message for now
        assert len(tokenized) == 2
        prompt_token_num = len(tokenized[0].tokens)
        output_token_num = len(tokenized[1].tokens)
    elif isinstance(tokenized[0], PromptMessage):
        # TODO: only support one prompt message for now
        assert len(tokenized) == 1
        prompt_token_num = len(tokenized[0].tokens)
        output_token_num = 0
    else:
        raise RuntimeError("wrong message type: %s" % type(tokenized[0]))

    if prompt_token_num + output_token_num > max_length:
        # only truncate prompt
        start_char_idx = end_char_idx = -1
        for i, token_id in enumerate(tokenized[0].tokens):
            if start_char_idx == -1 and token_id == start_char_token_id:
                start_char_idx = i
            if end_char_idx == -1 and token_id == end_char_token_id:
                end_char_idx = i
            if start_char_idx != -1 and end_char_idx != -1:
                break
        if start_char_idx == -1 or end_char_idx == -1:
            logging.warn("cannot find start_char_token_id[%s] or end_char_token_id[%s] in input_tokens[%s]" %
                            (start_char_token_id, end_char_token_id, tokenized[0].tokens))
            start_char_idx, end_char_idx = 0, len(tokenized[0].tokens) - 1
        middle_token_part = tokenized[0].tokens[start_char_idx+1:end_char_idx]
        if isinstance(tokenized[0], PromptMessage):
            middle_mask_part = tokenized[0].mask[start_char_idx+1:end_char_idx]
        middle_max_len = max_length - output_token_num - (prompt_token_num - len(middle_token_part))
        if middle_max_len < 0:
            raise RuntimeError(("please shorten the prompt or output, max_length: %d, prompt_token_num: %d, " +
                                "output_token_num: %d, middle_max_len: %d") %
                                (max_length, prompt_token_num, output_token_num, middle_max_len))
        if truncation_side == "middle-right":
            middle_token_part = middle_token_part[::-1]
            if isinstance(tokenized[0], PromptMessage):
                middle_mask_part = middle_mask_part[::-1]
        while len(middle_token_part) > middle_max_len:
            try:
                sep_char_idx = middle_token_part.index(sep_char_token_id)
                middle_token_part = middle_token_part[sep_char_idx+1:]
                if isinstance(tokenized[0], PromptMessage):
                    middle_mask_part = middle_mask_part[sep_char_idx+1:]
            except:
                middle_token_part = ()
                if isinstance(tokenized[0], PromptMessage):
                    middle_mask_part = ()
                break
        if truncation_side == "middle-right":
            middle_token_part = middle_token_part[::-1]
            if isinstance(tokenized[0], PromptMessage):
                middle_mask_part = middle_mask_part[::-1]
        tokenized[0].tokens = tokenized[0].tokens[:start_char_idx+1] + middle_token_part + tokenized[0].tokens[end_char_idx:]
        if isinstance(tokenized[0], PromptMessage):
            tokenized[0].mask = tokenized[0].mask[:start_char_idx+1] + middle_mask_part + tokenized[0].mask[end_char_idx:]

def tokenize_dialogue(  # noqa: C901
    dialogue: Union[str, Iterable[str]], tokenizer: Union[PreTrainedTokenizer, PreTrainedTokenizerFast], max_length=2048
) -> List[DialogMessage]:
    """
    Tokenize sample with the interleaved form of (prompt_1, output_1, prompt_2, output_2...)
    """
    if isinstance(dialogue, str):
        bos_token = tokenizer.bos_token or tokenizer.eos_token
        dialogue = [bos_token, dialogue]
    elif isinstance(dialogue, Iterable):
        if len(dialogue) % 2 != 0:
            raise ValueError("Dialogue must have an even number of phrases, alternating prompt and output")
        dialogue = list(dialogue)

    if not dialogue[-1].endswith(tokenizer.eos_token):
        dialogue[-1] = dialogue[-1] + tokenizer.eos_token

    tokenized = [
        DialogMessage(is_output=i % 2 == 1, tokens=tuple(tokenizer(dialogue[i], add_special_tokens=False).input_ids))
        for i in range(len(dialogue))
    ]

    # flip to truncate from the left
    if tokenizer.truncation_side == "left":
        tokenized = [DialogMessage(is_output=m.is_output, tokens=m.tokens[::-1]) for m in tokenized[::-1]]

    if tokenizer.truncation_side.startswith("middle"):
        middle_truncate(tokenized, max_length, tokenizer.truncation_side, tokenizer.start_char_token_id,
                        tokenizer.end_char_token_id, tokenizer.sep_char_token_id)
        truncated = tokenized
    else:
        # truncate if necessary
        lengths = [len(t.tokens) for t in tokenized]
        cumsum_lengths = [sum(lengths[:i]) for i in range(len(lengths))]
        truncated = [
            DialogMessage(is_output=t.is_output, tokens=t.tokens[: max(max_length - cl, 0)])
            for t, cl in zip(tokenized, cumsum_lengths)
        ]

    # flip back if was fliped to left truncate
    if tokenizer.truncation_side == "left":
        truncated = [DialogMessage(is_output=m.is_output, tokens=m.tokens[::-1]) for m in truncated[::-1]]

    # remove empty messages
    out = [t for t in truncated if len(t.tokens) > 0]

    if out[0].is_output:
        if sum(map(lambda msg: len(msg.tokens), out)) == max_length:
            if tokenizer.truncation_side == "left":
                out[0].tokens = out[0].tokens[1:]
            else:
                out[-1].tokens = out[-1].tokens[:-1]

        out.insert(0, DialogMessage(False, (tokenizer.bos_token_id,)))
    return out


class DialogStore(BaseRolloutStore):
    def __init__(self, dialogs: List[List[DialogMessage]], tokenizer: PreTrainedTokenizer):
        super().__init__()
        self.tokenizer = tokenizer
        attention_masks = [torch.ones(sum(len(m.tokens) for m in d), dtype=torch.bool) for d in dialogs]
        input_ids = [torch.tensor([t for m in d for t in m.tokens], dtype=torch.long) for d in dialogs]
        # -100 is the ignore index for CrossEntropyLoss
        labels = [
            torch.tensor([t if m.is_output else -100 for m in d for t in m.tokens], dtype=torch.long) for d in dialogs
        ]
        self.history = [
            dict(input_ids=i, attention_mask=a, labels=l) for i, a, l in zip(input_ids, attention_masks, labels)
        ]

    def create_loader(self, batch_size: int, shuffle=False) -> DataLoader:
        hf_collate_fn = DataCollatorWithPadding(self.tokenizer)

        def collate_fn(elems: Iterable[dict]):
            batch = hf_collate_fn(
                {"input_ids": [e["input_ids"] for e in elems], "attention_mask": [e["attention_mask"] for e in elems]}
            )
            labels = hf_collate_fn([{"input_ids": e["labels"]} for e in elems])["input_ids"]
            batch["labels"] = labels
            return batch

        return DataLoader(self, batch_size=batch_size, collate_fn=collate_fn, shuffle=shuffle)


class DialogStreamStore(BaseRolloutStreamStore):
    def __init__(self, dialogs_iter: List[List[DialogMessage]], data_size: int,
                 tokenizer: PreTrainedTokenizer, seq_length: int):
        super().__init__(dialogs_iter, data_size)
        self.tokenizer = tokenizer
        self.seq_length = seq_length
        if self.tokenizer.truncation_side.startswith("middle"):
            assert "middle_start_char" in self.tokenizer.init_kwargs
            assert "middle_end_char" in self.tokenizer.init_kwargs
            assert "middle_sep_char" in self.tokenizer.init_kwargs
            start_char_token_id = self.tokenizer(self.tokenizer.init_kwargs["middle_start_char"]).input_ids[-1]
            end_char_token_id = self.tokenizer(self.tokenizer.init_kwargs["middle_end_char"]).input_ids[-1]
            sep_char_token_id = self.tokenizer(self.tokenizer.init_kwargs["middle_sep_char"]).input_ids[-1]
            setattr(self.tokenizer, "start_char_token_id", start_char_token_id)
            setattr(self.tokenizer, "end_char_token_id", end_char_token_id)
            setattr(self.tokenizer, "sep_char_token_id", sep_char_token_id)

    def __getitem__(self, index: int):
        sample = self.iterator.__getitem__(index)
        dialog = tokenize_dialogue(sample, self.tokenizer, self.seq_length)
        attention_mask = torch.ones(sum(len(m.tokens) for m in dialog), dtype=torch.bool)
        input_id = torch.tensor([t for m in dialog for t in m.tokens], dtype=torch.long)
        # -100 is the ignore index for CrossEntropyLoss
        labels = torch.tensor([t if m.is_output else -100 for m in dialog for t in m.tokens], dtype=torch.long)
        data = dict(input_ids=input_id, attention_mask=attention_mask, labels=labels)
        return data

    def create_loader(self, batch_size: int, shuffle=False) -> DataLoader:
        hf_collate_fn = DataCollatorWithPadding(self.tokenizer)

        def collate_fn(elems: Iterable[dict]):
            batch = hf_collate_fn(
                {"input_ids": [e["input_ids"] for e in elems], "attention_mask": [e["attention_mask"] for e in elems]}
            )
            labels = hf_collate_fn([{"input_ids": e["labels"]} for e in elems])["input_ids"]
            batch["labels"] = labels
            return batch

        return DataLoader(self, batch_size=batch_size, collate_fn=collate_fn, shuffle=shuffle)


@register_datapipeline
class PromptPipeline(BasePipeline):
    """
    Dataloader which is used to supply prompts for either training or evaluation

    Args:
        prompts (`List[str]` or `List[Dict[str, Any]]`): list of raw text prompts or a dictionary with a required
            key `"prompt"` and extra information, that would be passed along the generation for that prompt as a
            keyword argument to a reward function.
        max_prompt_length (`int`): max length of the prompt, if exceeded the prompt will be truncated according to
            tokenizer's truncation setting.
        tokenizer (`transformers.PreTrainedTokenizer`): a tokenizer to tokenize prompts with.
        add_special_tokens (`bool`): whether to encode prompts with tokenizer's special tokens (passed directly
            into `tokenizer.encode`)
    """

    def __init__(
        self,
        prompts: Union[List[Dict[str, Any]], List[str]],
        max_prompt_length: int,
        tokenizer: PreTrainedTokenizer,
        add_special_tokens: bool = False,
    ):
        super().__init__()

        if isinstance(prompts[0], dict):
            metadata = prompts
            prompts = [x.pop("prompt") for x in metadata]
        else:
            metadata = [{}] * len(prompts)

        if tokenizer.truncation_side.startswith("middle"):
            start_char_token_id = tokenizer(tokenizer.init_kwargs["middle_start_char"]).input_ids[-1]
            end_char_token_id = tokenizer(tokenizer.init_kwargs["middle_end_char"]).input_ids[-1]
            sep_char_token_id = tokenizer(tokenizer.init_kwargs["middle_sep_char"]).input_ids[-1]
            prompts_tokens = []
            attention_mask = []
            for prompt in prompts:
                result = tokenizer(prompt, add_special_tokens=False)
                tokenized = [PromptMessage(tokens=tuple(result.input_ids), mask=tuple(result.attention_mask))]
                middle_truncate(tokenized, max_prompt_length, tokenizer.truncation_side,
                                start_char_token_id, end_char_token_id, sep_char_token_id)
                prompts_tokens.append(list(tokenized[0].tokens))
                attention_mask.append(list(tokenized[0].mask))
        else:
            model_inputs = tokenizer(
                prompts, truncation=True, padding=False, max_length=max_prompt_length, add_special_tokens=add_special_tokens
            )

            prompts_tokens = model_inputs["input_ids"]
            attention_mask = model_inputs["attention_mask"]

        self.tokenizer = tokenizer
        self.prompts = [
            {"input_ids": tokens, "attention_mask": mask, **metadata}
            for tokens, mask, metadata in zip(prompts_tokens, attention_mask, metadata)
        ]

    def __getitem__(self, ix: int):
        return self.prompts[ix]

    def __len__(self) -> int:
        return len(self.prompts)

    def create_loader(self, batch_size: int, shuffle=False, sampler=None, drop_last=False) -> DataLoader:
        def collate_fn(xs):
            out = self.tokenizer.pad([{"input_ids": x["input_ids"]} for x in xs], return_tensors="pt")

            for key in xs[0]:
                if key != "input_ids" and key != "attention_mask":
                    out[key] = [x[key] for x in xs]

            return out

        # Since all data is already pre-processed, no need to have
        # multi-process data loading
        return DataLoader(
            self,
            batch_size=batch_size,
            collate_fn=collate_fn,
            shuffle=shuffle,
            sampler=sampler,
            num_workers=0,
            drop_last=drop_last,
        )


def ilql_collate_fn(elems: Iterable[ILQLElement]):
    return ILQLBatch(
        pad_sequence([x.input_ids for x in elems], batch_first=True, padding_value=0),
        pad_sequence([x.attention_mask for x in elems], batch_first=True, padding_value=0),
        pad_sequence([x.rewards for x in elems], batch_first=True, padding_value=0.0),
        pad_sequence([x.states_ixs for x in elems], batch_first=True, padding_value=0),
        pad_sequence([x.actions_ixs for x in elems], batch_first=True, padding_value=0),
        pad_sequence([x.dones for x in elems], batch_first=True, padding_value=0),
    )


class ILQLRolloutStorage(BaseRolloutStore):
    """
    Rollout storage for training ILQL
    """

    def __init__(self, input_ids, attention_mask, rewards, states_ixs, actions_ixs, dones):
        super().__init__()

        self.input_ids = input_ids
        self.attention_mask = attention_mask
        self.rewards = rewards
        self.states_ixs = states_ixs
        self.actions_ixs = actions_ixs
        self.dones = dones

    def __getitem__(self, ix: int) -> ILQLElement:
        return ILQLElement(
            self.input_ids[ix],
            self.attention_mask[ix],
            self.rewards[ix],
            self.states_ixs[ix],
            self.actions_ixs[ix],
            self.dones[ix],
        )

    def __len__(self) -> int:
        return len(self.input_ids)

    def create_loader(self, batch_size: int):
        return DataLoader(
            self,
            batch_size=batch_size,
            shuffle=True,
            collate_fn=ilql_collate_fn,
            drop_last=torch.distributed.is_initialized(),
        )


def ilql_seq2seq_collate_fn(elems: Iterable[ILQLElement]):
    return ILQLSeq2SeqBatch(
        pad_sequence([x.input_ids for x in elems], batch_first=True, padding_value=0),
        pad_sequence([x.attention_mask for x in elems], batch_first=True, padding_value=0),
        pad_sequence([x.decoder_input_ids for x in elems], batch_first=True, padding_value=0),
        pad_sequence([x.rewards for x in elems], batch_first=True, padding_value=0.0),
        pad_sequence([x.states_ixs for x in elems], batch_first=True, padding_value=0),
        pad_sequence([x.actions_ixs for x in elems], batch_first=True, padding_value=0),
        pad_sequence([x.dones for x in elems], batch_first=True, padding_value=0),
    )


class ILQLSeq2SeqRolloutStorage(BaseRolloutStore):
    """
    Rollout storage for training ILQL with Seq2Seq models
    """

    def __init__(self, input_ids, attention_mask, decoder_input_ids, rewards, states_ixs, actions_ixs, dones):
        super().__init__()

        self.input_ids = input_ids
        self.attention_mask = attention_mask
        self.decoder_input_ids = decoder_input_ids
        self.rewards = rewards
        self.states_ixs = states_ixs
        self.actions_ixs = actions_ixs
        self.dones = dones

    def __getitem__(self, ix: int) -> ILQLElement:
        return ILQLSeq2SeqElement(
            self.input_ids[ix],
            self.attention_mask[ix],
            self.decoder_input_ids[ix],
            self.rewards[ix],
            self.states_ixs[ix],
            self.actions_ixs[ix],
            self.dones[ix],
        )

    def __len__(self) -> int:
        return len(self.input_ids)

    def create_loader(self, batch_size: int):
        return DataLoader(
            self,
            batch_size=batch_size,
            shuffle=True,
            collate_fn=ilql_seq2seq_collate_fn,
            drop_last=torch.distributed.is_initialized(),
        )
