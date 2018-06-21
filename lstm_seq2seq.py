import os
import csv
import string
import math
import linecache
import pickle
from collections import Counter

import spacy

from keras.utils import Sequence
import numpy as np

from model import SquadBaseline
from utils import get_spans


spacy_en = spacy.load('en_core_web_sm',
                      disable=['vectors', 'textcat', 'tagger', 'parser', 'ner'])


# class DotAttentionLayer(Layer):
#     def call(self, inputs):
#         keys, query = inputs
#         if len(K.int_shape(query)) == 2:
#             # when query is a vector
#             query = tf.expand_dims(query, dim=1)
#         scores = tf.matmul(query, keys, transpose_b=True)
#         # scores_mask = tf.expand_dims(tf.sequence_mask(
#         #     lengths=tf.to_int32(tf.squeeze(lengths, axis=1)),
#         #     maxlen=tf.to_int32(tf.shape(scores)[1]),
#         #     dtype=tf.float32), dim=2)
#         # scores = scores * scores_mask + (1. - scores_mask) * tf.float32.min
#         weights = K.softmax(scores, axis=2)
#         return tf.matmul(weights, keys)
#
#     def compute_mask(self, inputs, mask=None):
#         # just feeding query's mask
#         if mask is not None:
#             return mask[1]
#         else:
#             return None


def normalize_answer(text):
    def white_space_fix(text):
        return ' '.join(text.split())

    def remove_punc(text):
        exclude = set(string.punctuation)
        return ''.join(char for char in text if char not in exclude)

    return white_space_fix(remove_punc(str.lower(text)))


def f1_score(prediction, ground_truth):
    if prediction == ground_truth == '':
        return 1
    prediction_tokens = normalize_answer(prediction).split()
    ground_truth_tokens = normalize_answer(ground_truth).split()
    common = Counter(prediction_tokens) & Counter(ground_truth_tokens)
    num_same = sum(common.values())
    if num_same == 0:
        return 0
    precision = 1. * num_same / len(prediction_tokens)
    recall = 1. * num_same / len(ground_truth_tokens)
    f1 = (2 * precision * recall) / (precision + recall)
    return f1


def exact_match_score(prediction, ground_truth):
    return (normalize_answer(prediction) == normalize_answer(ground_truth))


def metric_max_over_ground_truths(metric_fn, prediction, ground_truths):
    scores_for_groud_truths = []
    for ground_truth in ground_truths:
        score = metric_fn(prediction, ground_truth)
        scores_for_groud_truths.append(score)
    return max(scores_for_groud_truths)


class SquadMetric:
    def __init__(self):
        self._total_em = 0.
        self._total_f1 = 0.
        self._count = 0

    def __call__(self, best_span_string, answer_string):
        em = metric_max_over_ground_truths(
            exact_match_score, best_span_string, [answer_string])
        f1 = metric_max_over_ground_truths(
            f1_score, best_span_string, [answer_string])
        self._total_em += em
        self._total_f1 += f1
        self._count += 1

    def get_metric(self, reset=False):
        em = self._total_em / self._count if self._count > 0 else 0
        f1 = self._total_f1 / self._count if self._count > 0 else 0
        if reset:
            self._total_em = 0.
            self._total_f1 = 0.
            self._count = 0
        return em, f1


class TextData:
    def __init__(self, data):
        self.data = data

    def to_array(self, token_to_index, unk_index, dtype=np.int32):
        self.data = np.array([[token_to_index.get(token, unk_index)
                               for token in x] for x in self.data]).astype(np.int32)
        return self

    def reshape(self, shape):
        self.data = self.data.reshape(shape)
        return self

    def getattr(self, attr):
        self.data = [[getattr(token, attr) for token in x] for x in self.data]
        return self

    def padding(self, pad_token):
        max_length = self.max_length
        self.data = [x + [pad_token] * (max_length - len(x)) for x in self.data]
        return self

    @property
    def max_length(self):
        return max(len(x) for x in self.data)

    def __len__(self):
        return len(self.data)


def tokenizer(x): return [token for token in spacy_en(x) if not token.is_space]


def make_vocab(tokens, max_size):
    counter = Counter(tokens)
    ordered_tokens, _ = zip(*counter.most_common())

    index_to_token = ('<pad>', '<unk>', '<s>', '</s>') + ordered_tokens
    if len(index_to_token) > max_size:
        index_to_token = index_to_token[: max_size]
    indices = range(len(index_to_token))
    token_to_index = dict(zip(index_to_token, indices))
    return token_to_index, list(index_to_token)


class SquadSequence(Sequence):
    def __init__(self, filename, batch_size):
        self._filename = filename
        with open(filename) as f:
            self._total_data = len(f.readlines()) - 1
        self._batch_size = batch_size
        self._indices = np.random.permutation(self._total_data)

    def __len__(self):
        return int(math.ceil(self._total_data / float(self._batch_size)))

    def __getitem__(self, idx):
        lines = []
        for i in self._indices[idx * self._batch_size:(idx + 1) * self._batch_size]:
            lines.append(linecache.getline(self._filename, i + 1))
        data = [row for row in csv.reader(lines, delimiter='\t')]
        contexts, questions, char_starts, char_ends, answers = zip(*data)

        contexts = [tokenizer(x) for x in contexts]
        questions = [tokenizer(x) for x in questions]
        char_starts = [int(x) for x in char_starts]
        char_ends = [int(x) for x in char_ends]
        question_batch = TextData(questions).getattr('text').padding('<pad>').to_array(token_to_index, 0).data
        context_batch = TextData(contexts).getattr('text').padding('<pad>').to_array(token_to_index, 0).data

        target_batch = np.zeros(context_batch.shape, dtype=np.int32)
        span_batch = np.zeros(context_batch.shape + (3,))
        spans = get_spans(contexts, char_starts, char_ends)
        for i, spans in enumerate(spans):
            if spans[0] >= 0:
                start = spans[0]
                end = spans[1]
                target_batch[i, start] = 1
                target_batch[i, start + 1: end + 1] = 2
                if i < len(spans):
                    span_batch[i + 1, start, 1] = 1.
                    span_batch[i + 1, start + 1: end + 1, 2] = 1.

        return [question_batch, context_batch, span_batch], target_batch[:, :, None]

    def on_epoch_end(self):
        self._indices = np.random.permutation(self._total_data)


class SquadTestGenerator:
    def __init__(self, filename, batch_size):
        self._filename = filename
        with open(filename) as f:
            self._total_data = len(f.readlines()) - 1
        self._batch_size = batch_size
        indices = range(self._total_data)
        self._indices = [indices[i:i + self._batch_size] for i in range(0, self._total_data, self._batch_size)]

    def __len__(self):
        return int(math.ceil(self._total_data / float(self._batch_size)))

    def __iter__(self):
        for indices in self._indices:
            contexts, questions, _, _, answers = zip(*csv.reader(
                [linecache.getline(self._filename, i + 1) for i in indices], delimiter='\t'))

            contexts = [tokenizer(x) for x in contexts]
            questions = [tokenizer(x) for x in questions]
            question_batch = TextData(questions).getattr('text').padding('<pad>').to_array(token_to_index, 0).data
            context_batch = TextData(contexts).getattr('text').padding('<pad>').to_array(token_to_index, 0).data

            yield question_batch, context_batch, answers


if not os.path.exists('vocab.pkl'):
    with open('data/squad_train_v2.0/train-v2.0.txt') as f:
        data = [row for row in csv.reader(f, delimiter='\t')]
    data = [[tokenizer(x[0]), tokenizer(x[1]), int(x[2]), int(x[3]), x[4]]
            for x in data]
    contexts, questions, char_starts, char_ends, answers = zip(*data)
    tokens = (token.text for tokens in contexts + questions for token in tokens)
    token_to_index, index_to_token = make_vocab(tokens, 30000)
    with open('vocab.pkl', mode='wb') as f:
        pickle.dump((token_to_index, index_to_token), f)
else:
    with open('vocab.pkl', mode='rb') as f:
        token_to_index, index_to_token = pickle.load(f)

batch_size = 256  # Batch size for training.
epochs = 100  # Number of epochs to train for.
latent_dim = 128  # Latent dimensionality of the encoding space.
num_encoder_tokens = len(token_to_index)
num_decoder_tokens = 3


print('Number of unique input tokens:', num_encoder_tokens)
print('Number of unique output tokens:', num_decoder_tokens)

decoder_token_to_index = {'ignore': 0, 'start': 1, 'keep': 2}
decoder_index_to_token = ['ignore', 'start', 'keep']

# encoder_inputs = Input(shape=(None,))
# embedding = Embedding(len(token_to_index), latent_dim, mask_zero=True)
# encoder = LSTM(latent_dim, return_state=True, return_sequences=True)
# encoder_outputs, state_h, state_c = encoder(embedding(encoder_inputs))
# encoder_states = [state_h, state_c]
#
#
# decoder_inputs = Input(shape=(None,))
# decoder_inputs2 = Input(shape=(None, 3))
# decoder_lstm = LSTM(latent_dim, return_sequences=True, return_state=True)
# decoder_dense = Dense(num_decoder_tokens, activation='softmax')
# concat = Concatenate(axis=-1)
# decoder_outputs, _, _ = decoder_lstm(concat([embedding(decoder_inputs), decoder_inputs2]),
#                                      initial_state=encoder_states)
# attention = DotAttentionLayer()
# attention_outputs = attention([encoder_outputs, decoder_outputs])
# decoder_outputs = decoder_dense(concat([decoder_outputs, attention_outputs]))
#
#
# model = Model([encoder_inputs, decoder_inputs, decoder_inputs2], decoder_outputs)

model, inference = SquadBaseline(len(token_to_index), latent_dim, latent_dim, 3).build()
model.compile(optimizer='adam', loss='sparse_categorical_crossentropy')
train_generator = SquadSequence('data/train-v2.0.txt', batch_size)
model.fit_generator(
    generator=train_generator, steps_per_epoch=len(train_generator), epochs=epochs,
    use_multiprocessing=True)
model.save('s2s.h5')


# encoder_model = Model(encoder_inputs, [encoder_outputs] + encoder_states)
#
# # input placeholder
# decoder_state_input_h = Input(shape=(latent_dim,))
# decoder_state_input_c = Input(shape=(latent_dim,))
# encoder_outputs_inputs = Input(shape=(None, latent_dim))
# decoder_states_inputs = [decoder_state_input_h, decoder_state_input_c]
# # feeding lstm
# decoder_outputs, state_h, state_c = decoder_lstm(
#     concat([embedding(decoder_inputs), decoder_inputs2]), initial_state=decoder_states_inputs)
# attention_outputs = attention([encoder_outputs_inputs, decoder_outputs])
# # model outputs
# decoder_states = [state_h, state_c]
# decoder_outputs = decoder_dense(concat([decoder_outputs, attention_outputs]))
# decoder_model = Model(
#     [decoder_inputs, decoder_inputs2, encoder_outputs_inputs] + decoder_states_inputs,
#     [decoder_outputs] + decoder_states)
#
#
# def decode_sequence(question_seq, context_seq, batch_size):
#     # Encode the input as state vectors.
#     encoder_outputs, *states_value = encoder_model.predict([question_seq])
#
#     decoded_tokens = []
#     action = np.zeros((batch_size, 1, 3))
#     for token in np.transpose(context_seq, [1, 0]):
#         output_tokens, h, c = decoder_model.predict(
#             [token, action, encoder_outputs] + states_value)
#         output_tokens = np.squeeze(output_tokens)
#         sampled_token_indices = np.argmax(output_tokens, axis=1).tolist()
#         sampled_char = [decoder_index_to_token[i] for i in sampled_token_indices]
#         decoded_tokens.append(sampled_char)
#
#         action = np.identity(3)[sampled_token_indices][:, None, :]
#
#         states_value = [h, c]
#
#     return decoded_tokens


metric = SquadMetric()
dev_generator = SquadTestGenerator('data/dev-v2.0.txt', batch_size)
for question, context, answer in dev_generator:
    decoded_sentences = inference(question, context)
    for i, sent in enumerate(zip(*decoded_sentences)):
        indices = [j for j, y in enumerate(sent) if y == 1 or y == 2]
        prediction = ' '.join(index_to_token[context[i][j]] for j in indices)
        metric(prediction, answer[i])
print('EM: {}, F1: {}'.format(*metric.get_metric()))
