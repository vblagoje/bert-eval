import copy
import glob
import os
import random
import sys

import torch
from transformers import AutoTokenizer, AutoModelForMaskedLM


class BertGeneration(object):

    def __init__(self, model_name, tokenizer):

        # Load pre-trained model (weights)

        self.model = AutoModelForMaskedLM.from_pretrained(model_name)
        self.model.eval()
        self.cuda = torch.cuda.is_available()
        if self.cuda:
            self.model = self.model.cuda()

        # Load pre-trained model tokenizer (vocabulary)
        if tokenizer:
            self.tokenizer = AutoTokenizer.from_pretrained(tokenizer)
        else:
            self.tokenizer = AutoTokenizer.from_pretrained(model_name)

        self.CLS = '[CLS]'
        self.SEP = '[SEP]'
        self.MASK = '[MASK]'
        self.mask_id = self.tokenizer.convert_tokens_to_ids([self.MASK])[0]
        self.sep_id = self.tokenizer.convert_tokens_to_ids([self.SEP])[0]
        self.cls_id = self.tokenizer.convert_tokens_to_ids([self.CLS])[0]

    def tokenize_batch(self, batch):
        return [self.tokenizer.convert_tokens_to_ids(sent) for sent in batch]

    def untokenize_batch(self, batch):
        return [self.tokenizer.convert_ids_to_tokens(sent) for sent in batch]

    def detokenize(self, sent):
        """ Roughly detokenizes (mainly undoes wordpiece) """
        new_sent = []
        for i, tok in enumerate(sent):
            if tok.startswith("##"):
                new_sent[len(new_sent) - 1] = new_sent[len(new_sent) - 1] + tok[2:]
            else:
                new_sent.append(tok)
        return new_sent

    def printer(self, sent, should_detokenize=True):
        if should_detokenize:
            sent = self.detokenize(sent)[1:-1]
        print(" ".join(sent))

    def predict_masked(self, sent):
        tokens = ['[CLS]'] + sent + ['[SEP]']
        target_indices = [i for i, x in enumerate(tokens) if x == '[MASK]']
        input_ids = self.tokenizer.convert_tokens_to_ids(tokens)
        tens = torch.LongTensor(input_ids).unsqueeze(0)
        if self.cuda:
            tens = tens.cuda()
        try:
            res = self.model(tens)[0]
        except RuntimeError:  # Error in the model vocabulary, remove when a corret model is trained
            return None
        target_tensor = torch.LongTensor(target_indices)
        if self.cuda:
            target_tensor = target_tensor.cuda()
        res = (torch.index_select(res, 1, target_tensor))
        res = torch.narrow(torch.argsort(res, dim=-1, descending=True), -1, 0, 5)

        predicted = []
        for mask in res[0,]:
            candidates = self.tokenizer.convert_ids_to_tokens([i.item() for i in mask])

            predicted.append(candidates)

        return predicted


class DataMangler(object):

    def __init__(self, file_name, min_len, max_len, max_sent):

        self.sentences = self.read_sentences(file_name, min_len, max_len)
        self.max_eval_sentences = max_sent

    def read_sentences(self, file_name, min_len, max_len):
        sentences = []
        with open(file_name, "rt", encoding="utf-8") as f:
            for line in f:
                if len(line.split(" ")) < min_len or len(line.split(" ")) > max_len:
                    continue
                sentences.append(line.strip())
        random.shuffle(sentences)
        return sentences

    def glue_tokenized(self, tokenized):
        tokenized_words = []
        for subword in tokenized:
            if not subword.startswith("##"):  # new word starts
                tokenized_words.append([])
            tokenized_words[-1].append(subword)
        return tokenized_words

    def random_mask(self, sent, p=0.15):
        if len(sent) > 512 - 2:  # bert max seq len
            return None
        num_tokens = int(round(len(sent) * p))
        if num_tokens == 0:
            return None
        indices = random.sample(range(len(sent)), num_tokens)
        return indices

    def mask_sent(self, sent, mask_indices):
        masked_sentence = []
        for i, token in enumerate(sent):
            for subword in token:
                if i in mask_indices:
                    masked_sentence.append("[MASK]")
                else:
                    masked_sentence.append(subword)
        return masked_sentence

    def unmask_sent(self, sent, mask_indices, predicted, mark=True):
        unmasked_sentence = []
        for i, token in enumerate(sent):
            for j, subword in enumerate(token):
                if i in mask_indices:
                    p_ = predicted.pop(0)[0]
                    if j == 0:
                        p_ = "**" + p_
                    if j == len(token) - 1:
                        p_ += "**"
                    unmasked_sentence.append(p_)
                else:
                    unmasked_sentence.append(subword)
        return self.my_detokenizer(unmasked_sentence)

    def my_detokenizer(self, sent):
        new_sent = []
        for i, tok in enumerate(sent):
            if tok.startswith("##"):
                new_sent[len(new_sent) - 1] = new_sent[len(new_sent) - 1] + tok[2:]
            elif tok.startswith("**##"):
                new_sent[len(new_sent) - 1] = new_sent[len(new_sent) - 1] + "**" + tok[4:]
            else:
                new_sent.append(tok)

        return " ".join(new_sent).replace("****", "").replace("** **", " ")

    def compare_subwords(self, glued_tokenized, predicted, mask_index):
        correct = 0
        for i, word in enumerate(glued_tokenized):
            if i in mask_index:
                for j, subword in enumerate(word):
                    pred = predicted.pop(0)[0]
                    if subword == pred:
                        correct += 1
        return correct

    def predict_iterator(self, bert_model):

        counter = 0

        for sentence in self.sentences:

            tokenized_sentence = bert_model.tokenizer.tokenize(sentence)

            glued = self.glue_tokenized(tokenized_sentence)

            mask_index = self.random_mask(glued)
            if mask_index == None:  # sentence is too short
                continue
            masked_sentence = self.mask_sent(glued, mask_index)

            # run bert
            predicted = bert_model.predict_masked(masked_sentence)
            if not predicted:
                continue

            correct_subwords = self.compare_subwords(glued, copy.copy(predicted),
                                                     mask_index)  # number of correctly predicted subwords
            total_subwords = sum([len(glued[mi]) for mi in mask_index])  # number of subwords

            yield correct_subwords, total_subwords, (
                " ".join(masked_sentence), sentence, self.unmask_sent(glued, mask_index, predicted, mark=True))
            counter += 1

            if self.max_eval_sentences != 0 and counter > self.max_eval_sentences:
                break


def main(args):
    correct_subwords = 0
    total_subwords = 0
    total_accuracy = 0

    print(f"Loading language model from {args.model}")
    bert_model = BertGeneration(args.model, args.tokenizer)

    target_files = os.path.join(args.input_dir, "*.txt")
    input_files = glob.glob(target_files)
    print(f"In {args.input_dir} there are {len(input_files)} input files for masked prediction")

    for input_file in sorted(input_files):
        print(f"Loading {input_file} for masked language prediction...")
        dataset = DataMangler(input_file, args.min_len, args.max_len, args.max_sentences)
        for correct_, total_, prediction_ in dataset.predict_iterator(bert_model):

            correct_subwords += correct_
            total_subwords += total_

            if args.verbose:
                print("Input:", prediction_[0], file=sys.stdout)
                print("Orig:", prediction_[1], file=sys.stdout)
                print("Pred:", prediction_[2], file=sys.stdout)
                print(file=sys.stdout)
        accuracy = correct_subwords / total_subwords
        print("Correct:", correct_subwords, "Total:", total_subwords, "Accuracy:", accuracy * 100)
        total_accuracy += accuracy
    print("Final accuracy:", total_accuracy / len(input_files))


if __name__ == "__main__":
    import argparse

    argparser = argparse.ArgumentParser(description='')
    argparser.add_argument('--model', required=True, type=str,
                           help='HuggingFace model (either local dir or remote model URL)')
    argparser.add_argument('--tokenizer', type=str,
                           help='HuggingFace model tokenizer')
    argparser.add_argument('--input_dir', required=False, type=str, default='./data/',
                           help='Directory with *.txt input files used for masking and prediction.')
    argparser.add_argument('--min_len', default=5, type=int,
                           help='Minumum sentence length used in evaluation')
    argparser.add_argument('--max_len', default=50, type=int,
                           help='Maximum sentence length used in evaluation')
    argparser.add_argument('--max_sentences', default=0, type=int,
                           help='How many sentences to use in evaluation (Default: 0, use all))')
    argparser.add_argument('--verbose', default=False, action="store_true",
                           help='Print the original and predicted sentences.')
    args = argparser.parse_args()

    main(args)
