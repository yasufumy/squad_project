import tensorflow as tf
from keras import backend as K
from keras import Model
from keras.regularizers import l2
from keras.initializers import VarianceScaling
from keras.layers import Input, Embedding, Concatenate, Lambda, \
    Conv1D, Masking, LSTM, Bidirectional, Dense

from layers import ContextQueryAttention, Highway, Encoder


regularizer = l2(3e-7)
VarianceScaling(scale=1., mode='fan_in', distribution='normal')


def mask_logits(inputs, mask, mask_value=tf.float32.min, axis=1, time_dim=1):
    mask = K.cast(mask, tf.int32)
    mask = K.one_hot(mask[:, 0], K.shape(inputs)[time_dim])
    mask = 1 - K.cumsum(mask, 1)
    mask = tf.cast(mask, tf.float32)
    if axis != 0:
        mask = tf.expand_dims(mask, axis)
    return inputs + mask_value * (1 - mask)


class QANet:
    def __init__(self, vocab_size, embed_size, filters=128, num_heads=1,
                 cont_limit=400, ques_limit=50,
                 dropout=0.1, encoder_num_blocks=1, encoder_num_convs=4,
                 output_num_blocks=2, output_num_convs=2, embeddings=None):
        self.cont_limit = cont_limit
        self.ques_limit = ques_limit
        if embeddings is not None:
            embeddings = [embeddings]
        self.embed_layer = Embedding(
            vocab_size, embed_size, weights=embeddings, trainable=False)
        self.highway = Highway(embed_size, 2, dropout=dropout, regularizer=regularizer)
        self.projection1 = Conv1D(filters, 1, padding='same', activation='linear',
                                  kernel_regularizer=regularizer)
        self.encoder = Encoder(filters, 7, encoder_num_blocks, encoder_num_convs, num_heads, dropout, regularizer)

        self.coattention = ContextQueryAttention(filters * 4, cont_limit, ques_limit, dropout, regularizer)
        self.projection2 = Conv1D(filters, 1, padding='same', activation='linear',
                                  kernel_regularizer=regularizer)

        self.output_layer = Encoder(filters, 5, output_num_blocks, output_num_convs, num_heads, dropout, regularizer)
        self.start_layer = Conv1D(1, 1, activation='linear', kernel_regularizer=regularizer)
        self.end_layer = Conv1D(1, 1, activation='linear', kernel_regularizer=regularizer)

    def build(self):
        cont_input = Input((self.cont_limit,))
        ques_input = Input((self.ques_limit,))

        # mask
        c_mask = Lambda(lambda x: tf.cast(x, tf.bool))(cont_input)
        q_mask = Lambda(lambda x: tf.cast(x, tf.bool))(ques_input)
        cont_len = Lambda(lambda x: tf.expand_dims(tf.reduce_sum(tf.cast(x, tf.int32), axis=1), axis=1))(c_mask)
        ques_len = Lambda(lambda x: tf.expand_dims(tf.reduce_sum(tf.cast(x, tf.int32), axis=1), axis=1))(q_mask)

        # encoding each
        x_cont = self.embed_layer(cont_input)
        x_cont = self.highway(x_cont)
        x_cont = self.projection1(x_cont)
        x_cont = self.encoder([x_cont, cont_len])

        x_ques = self.embed_layer(ques_input)
        x_ques = self.highway(x_ques)
        x_ques = self.projection1(x_ques)
        x_ques = self.encoder([x_ques, ques_len])

        x, S_bar, S_T = self.coattention([x_cont, x_ques, cont_len, ques_len])
        x = self.projection2(x)

        outputs = []
        for _ in range(3):
            x = self.output_layer([x, cont_len])
            outputs.append(x)

        x_start = Concatenate()([outputs[0], outputs[1]])
        x_start = self.start_layer(x_start)
        x_start = Lambda(lambda x: tf.squeeze(x, axis=-1))(x_start)
        x_start = Lambda(lambda x: mask_logits(x[0], x[1], axis=0, time_dim=1))([x_start, cont_len])
        x_start = Lambda(lambda x: K.softmax(x), name='start')(x_start)

        x_end = Concatenate()([outputs[0], outputs[2]])
        x_end = self.end_layer(x_end)  # batch * seq_len * 1
        x_end = Lambda(lambda x: tf.squeeze(x, axis=-1))(x_end)
        x_end = Lambda(lambda x: mask_logits(x[0], x[1], axis=0, time_dim=1))([x_end, cont_len])
        x_end = Lambda(lambda x: K.softmax(x), name='end')(x_end)  # batch * seq_len

        return Model(inputs=[ques_input, cont_input], outputs=[x_start, x_end])


class DependencyQANet:
    def __init__(self, vocab_size, embed_size, output_size, filters=128, num_heads=8,
                 ques_limit=50, dropout=0.1, num_blocks=1, num_convs=4, embeddings=None):
        self.ques_limit = ques_limit
        self.num_blocks = num_blocks
        self.num_convs = num_convs
        self.dropout = dropout
        if embeddings is not None:
            embeddings = [embeddings]
        self.embed_layer = Embedding(
            vocab_size, embed_size, weights=embeddings, trainable=False)
        self.highway = Highway(embed_size, 2, dropout=dropout, regularizer=regularizer)
        self.projection = Conv1D(
            filters, 1, activation='linear', kernel_regularizer=regularizer)
        self.encoder = Encoder(filters, 7, num_blocks, num_convs, num_heads, dropout, regularizer)
        self.output_layer = Conv1D(output_size, 1, activation='linear', kernel_regularizer=regularizer)

    def build(self):
        ques_input = Input((self.ques_limit,))

        # mask
        q_mask = Lambda(lambda x: tf.cast(x, tf.bool))(ques_input)
        ques_len = Lambda(lambda x: tf.expand_dims(tf.reduce_sum(tf.cast(x, tf.int32), axis=1), axis=1))(q_mask)

        # encoding each
        x_ques = self.embed_layer(ques_input)
        x_ques = self.highway(x_ques)
        x_ques = self.projection(x_ques)
        x_ques = self.encoder([x_ques, ques_len])

        y = self.output_layer(x_ques)  # batch * seq_len * output_size

        def mask_sequence(x, length):
            mask = tf.expand_dims(tf.sequence_mask(
                tf.squeeze(length, axis=1), maxlen=self.ques_limit, dtype=tf.float32), dim=2)
            return x * mask

        y = Lambda(lambda x: K.softmax(x), name='end')(y)  # batch * seq_len
        y = Lambda(lambda x: mask_sequence(x[0], x[1]))([y, ques_len])
        y = Masking(mask_value=0.)(y)

        return Model(inputs=ques_input, outputs=y)


class DependencyLSTM:
    def __init__(self, vocab_size, embed_size, output_size, hidden_size=128,
                 ques_limit=50, dropout=0.1, embeddings=None):
        self.ques_limit = ques_limit
        if embeddings is not None:
            embeddings = [embeddings]
        self.embed_layer = Embedding(
            vocab_size, embed_size, weights=embeddings, trainable=False, mask_zero=True)
        self.lstm = Bidirectional(LSTM(hidden_size, return_sequences=True))
        self.output_layer = Dense(output_size, activation='softmax')

    def build(self):
        ques_input = Input((self.ques_limit,))

        # encoding each
        x_ques = self.embed_layer(ques_input)
        x_ques = self.lstm(x_ques)

        y = self.output_layer(x_ques)  # batch * seq_len * output_size

        return Model(inputs=ques_input, outputs=y)
