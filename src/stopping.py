import os
import time

import torch
from transformers import StoppingCriteria, StoppingCriteriaList, GenerationConfig

from enums import PromptType, t5_type, extra_stop_token_ids


def update_terminate_responses(terminate_response, tokenizer=None, trust_remote_code=True):
    # FIXME: make trust_remote_code passed in from above, but generation config should be relatively safe
    if terminate_response is None:
        terminate_response = []
    if tokenizer is not None:
        # e.g. for dbrx
        if hasattr(tokenizer, 'added_tokens_encoder') and '<|im_end|>' in tokenizer.added_tokens_encoder:
            terminate_response.extend(['<|im_end|>'])
        if hasattr(tokenizer, 'eos_token') and tokenizer.eos_token:
            if isinstance(tokenizer.eos_token, str):
                terminate_response.extend([tokenizer.eos_token])
            elif isinstance(tokenizer.eos_token, list):
                terminate_response.extend(tokenizer.eos_token)

        if hasattr(tokenizer, 'name_or_path') and hasattr(tokenizer, 'vocab'):
            reverse_vocab = {v: k for k, v in tokenizer.vocab.items()}
            try:
                generate_eos_token_id = GenerationConfig.from_pretrained(tokenizer.name_or_path,
                                                                         token=os.getenv('HUGGING_FACE_HUB_TOKEN'),
                                                                         trust_remote_code=trust_remote_code,

                                                                         ).eos_token_id
                if isinstance(generate_eos_token_id, list):
                    for eos_token_id in generate_eos_token_id:
                        terminate_response.extend([reverse_vocab[eos_token_id]])
                elif generate_eos_token_id is not None:
                    terminate_response.extend([reverse_vocab[generate_eos_token_id]])
            except OSError:
                pass
        terminate_response_tmp = terminate_response.copy()
        terminate_response.clear()
        [terminate_response.append(x) for x in terminate_response_tmp if x not in terminate_response]
    return terminate_response


class StoppingCriteriaSub(StoppingCriteria):

    def __init__(self, stops=[], stop_words=[], encounters=[], device="cuda", model_max_length=None, tokenizer=None,
                 truncation_generation=False, max_time=None):
        super().__init__()
        assert len(stops) % len(encounters) == 0, "Number of stops and encounters must match"
        self.encounters = encounters
        self.stops = [stop.to(device) for stop in stops]
        self.stop_words = stop_words
        self.num_stops = [0] * len(stops)
        self.model_max_length = model_max_length
        self.tokenizer = tokenizer
        self.truncation_generation = truncation_generation
        self.token_start = None
        # not setup for handling existing prompt, only look at new tokens, some models like xwin have funny token handling,
        # and despite new tokens present the block looks back into different sized output and matches the stop token
        self.look_at_new_tokens_only = max(self.encounters) == 1
        self.max_time = max_time
        self.t0 = time.time()

    def __call__(self, input_ids: torch.LongTensor, scores: torch.FloatTensor, **kwargs) -> bool:
        if self.max_time is not None and (time.time() - self.t0) > self.max_time:
            print("Stopping: Took too long: %s" % self.max_time)
            return True
        # if self.tokenizer:
        #    print('stop: %s' % self.tokenizer.decode(input_ids[0]), flush=True)
        if self.token_start is None:
            self.token_start = input_ids[0].shape[0]
        if self.look_at_new_tokens_only:
            new_tokens = input_ids[0][self.token_start:]
        else:
            new_tokens = input_ids[0][0:]
        for stopi, (stop, stop_word) in enumerate(zip(self.stops, self.stop_words)):
            current_block = new_tokens[-len(stop):]
            stop_text = self.tokenizer.decode(current_block)
            len_new_tokens = current_block.shape[0]
            # if len(stop) <= len_new_tokens and torch.all((stop == input_ids[0][-len(stop):])).item():
            if len(stop) <= len_new_tokens and stop_word in stop_text:
                self.num_stops[stopi] += 1
                if self.num_stops[stopi] >= self.encounters[stopi % len(self.encounters)]:
                    # print("Stopped", flush=True)
                    return True
        if self.truncation_generation and (
                self.model_max_length is not None and input_ids[0].shape[0] >= self.model_max_length):
            # critical limit
            # print("Stopped 2", flush=True)
            return True
        # print("Tokens: %s: %s" % (len(input_ids[0].cpu().numpy()), input_ids[0].cpu().numpy()), flush=True)
        # print("Stop Tokens: %s" % [x.cpu().numpy() for x in self.stops], flush=True)
        # print("Not stopping", flush=True)
        return False


def get_stopping(prompt_type, prompt_dict, tokenizer, device, base_model,
                 human='<human>:', bot="<bot>:", model_max_length=None,
                 prompter=None,
                 stop=None,
                 truncation_generation=False,
                 max_time=None):
    stop_words = []
    encounters = []
    # FIXME: prompt_dict unused currently
    user_human_assistant_types = [PromptType.instruct_vicuna.value, str(PromptType.instruct_vicuna.value),
                                  PromptType.instruct_vicuna.name] + \
                                 [PromptType.guanaco.value, str(PromptType.guanaco.value),
                                  PromptType.guanaco.name] + \
                                 [PromptType.one_shot.value, str(PromptType.one_shot.value),
                                  PromptType.one_shot.name] + \
                                 [PromptType.instruct_vicuna2.value, str(PromptType.instruct_vicuna2.value),
                                  PromptType.instruct_vicuna2.name] + \
                                 [PromptType.instruct_vicuna3.value, str(PromptType.instruct_vicuna3.value),
                                  PromptType.instruct_vicuna3.name] + \
                                 [PromptType.instruct_with_end.value, str(PromptType.instruct_with_end.value),
                                  PromptType.instruct_with_end.name]
    human_bot_types = [PromptType.human_bot.value, str(PromptType.human_bot.value),
                       PromptType.human_bot.name] + \
                      [PromptType.human_bot_orig.value, str(PromptType.human_bot_orig.value),
                       PromptType.human_bot_orig.name]
    all_types = user_human_assistant_types + human_bot_types
    if prompt_type in all_types:
        if prompt_type in human_bot_types:
            # encounters = [prompt.count(human) + 1, prompt.count(bot) + 1]
            # stopping only starts once output is beyond prompt
            # 1 human is enough to trigger, but need 2 bots, because very first view back will be bot we added
            stop_words = [human, bot, '\n' + human, '\n' + bot]
            encounters = [1, 2]
        elif prompt_type in user_human_assistant_types:
            # even below is not enough, generic strings and many ways to encode
            stop_words = [
                '### Human:',
                """
### Human:""",
                """
### Human:
""",
                """###  Human:  """,
                """###  Human:""",
                '### Assistant:',
                """
### Assistant:""",
                """
### Assistant:
""",
                """###  Assistant:  """,
                """###  Assistant:"""
            ]
            if prompt_type in [PromptType.instruct_vicuna2.value, str(PromptType.instruct_vicuna2.value),
                               PromptType.instruct_vicuna2.name]:
                stop_words = [x.upper() for x in stop_words]
            if prompt_type in [PromptType.instruct_vicuna3.value, str(PromptType.instruct_vicuna3.value),
                               PromptType.instruct_vicuna3.name]:
                stop_words = [x.replace('Human', 'User') for x in stop_words]
            encounters = [1, 2]
        else:
            # some instruct prompts have this as end, doesn't hurt to stop on it since not common otherwise
            stop_words = ['### End']
            encounters = [1]
    elif prompter and prompter.terminate_response:
        stop_words = prompter.terminate_response
        encounters = [1] * len(stop_words)
    handle_newlines = [True] * len(stop_words)

    # add other stop words too if passed, e.g. for LangChain agents
    if stop:
        stop_words += stop
        encounters += [1] * len(stop)
        handle_newlines += [False] * len(stop)

    stop_words = update_terminate_responses(stop_words, tokenizer=tokenizer)
    stop_words.extend(extra_stop_token_ids(base_model, as_ids=False))

    # get stop tokens
    stop_words_ids = [
        tokenizer(stop_word, return_tensors='pt')['input_ids'].squeeze() for stop_word in stop_words]
    # handle single token case
    stop_words_ids = [x if len(x.shape) > 0 else torch.tensor([x]) for x in stop_words_ids]
    stop_words_ids = [x for x in stop_words_ids if x.shape[0] > 0]
    # avoid padding in front of tokens
    if hasattr(tokenizer, '_pad_token') and tokenizer._pad_token:  # use hidden variable to avoid annoying properly logger bug
        stop_words_ids = [x[1:] if x[0] == tokenizer.pad_token_id and len(x) > 1 else x for x in stop_words_ids]
    if hasattr(tokenizer, '_unk_token') and tokenizer._unk_token:  # use hidden variable to avoid annoying properly logger bug
        stop_words_ids = [x[1:] if x[0] == tokenizer.unk_token_id and len(x) > 1 else x for x in stop_words_ids]
        stop_words_ids = [x[:-1] if x[-1] == tokenizer.unk_token_id and len(x) > 1 else x for x in stop_words_ids]
    if hasattr(tokenizer, '_eos_token') and tokenizer._eos_token:  # use hidden variable to avoid annoying properly logger bug
        stop_words_ids = [x[:-1] if x[-1] == tokenizer.eos_token_id and len(x) > 1 else x for x in stop_words_ids]
    if hasattr(tokenizer, '_bos_token') and tokenizer._bos_token:  # use hidden variable to avoid annoying properly logger bug
        stop_words_ids = [x[1:] if x[0] == tokenizer.bos_token_id and len(x) > 1 else x for x in stop_words_ids]
        stop_words_ids = [x[:-1] if x[-1] == tokenizer.bos_token_id and len(x) > 1 else x for x in stop_words_ids]
    if base_model and t5_type(base_model) and hasattr(tokenizer, 'vocab'):
        # T5 encoder converts internal double space to space+new line, so fix
        for stopi, stop_word_id in enumerate(stop_words_ids):
            start = stop_word_id[0:1]
            mlist = stop_word_id[1:-1]
            end = stop_word_id[-1:]
            mlist = [tokenizer.vocab[' '] if x == tokenizer.vocab['\n'] else x for x in mlist]
            stop_words_ids[stopi] = torch.tensor(list(start) + list(mlist) + list(end), device=stop_word_id.device)
    # handle fake \n added
    stop_words_ids = [x[1:] if y[0] == '\n' and handle_newline else x for x, y, handle_newline in
                      zip(stop_words_ids, stop_words, handle_newlines)]
    if stop_words_ids:
        # build stopper
        stopping_criteria = StoppingCriteriaList(
            [StoppingCriteriaSub(stops=stop_words_ids,
                                 stop_words=stop_words,
                                 encounters=encounters, device=device,
                                 model_max_length=model_max_length, tokenizer=tokenizer,
                                 truncation_generation=truncation_generation,
                                 max_time=max_time)])
    else:
        # nothing to stop on
        stopping_criteria = StoppingCriteriaList()
    return stopping_criteria
