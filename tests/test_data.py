from unittest.mock import patch, mock_open
from unittest import TestCase
from data import make_vocab, SquadReader, SquadIterator, SquadConverter


class TestData(TestCase):
    def test_make_vocab(self):
        tokens = ['Rock', 'n', 'Roll', 'is', 'a', 'risk', '.', 'You', 'rick',
                  'being', 'ridiculed', '.']
        token_to_index, index_to_token = make_vocab(tokens, 1, 10)

        self.assertEqual(token_to_index['<pad>'], 0)
        self.assertEqual(token_to_index['<unk>'], 1)
        self.assertEqual(token_to_index['<s>'], 2)
        self.assertEqual(token_to_index['</s>'], 3)
        self.assertEqual(len(token_to_index), 10)
        self.assertEqual(len(index_to_token), 10)
        self.assertEqual(index_to_token[0], '<pad>')
        self.assertEqual(index_to_token[1], '<unk>')
        self.assertEqual(index_to_token[2], '<s>')
        self.assertEqual(index_to_token[3], '</s>')


class TestSquadReader(TestCase):
    def setUp(self):
        read_data = 'context1\tquestion1\tstart1\tend1\tanswer1\n' \
            'context2\tquestion2\tstart2\tend2\tanswer2'
        self.filename = 'path/to/target.tsv'
        self.lines = read_data.split('\n')
        self.mock_open = patch('data.open', mock_open(read_data=read_data)).start()
        self.mock_getline = patch('data.linecache.getline').start()
        self.dataset = SquadReader(self.filename)

    def test_init(self):
        self.assertEqual(self.dataset._filename, self.filename)
        self.assertEqual(self.dataset._total_data, 1)

    def test_len(self):
        self.assertEqual(len(self.dataset), 1)

    def test_getitem(self):
        for i, line in enumerate(self.lines):
            self.mock_getline.return_value = line
            self.assertListEqual(self.dataset[i], line.split('\t'))
            self.mock_getline.assert_called_with(self.filename, i + 1)

    def tearDown(self):
        patch.stopall()


class TestSquadIterator(TestCase):
    def setUp(self):
        dataset = range(100)
        self.batch_size = 32

        def converter(x): return x

        self.generator = SquadIterator(dataset, self.batch_size, converter)
        self.dataset = dataset
        self.converter = converter

    def test_init(self):
        self.assertEqual(self.generator._dataset, self.dataset)
        self.assertEqual(self.generator._batch_size, self.batch_size)
        self.assertEqual(self.generator._converter, self.converter)
        self.assertEqual(self.generator._current_position, 0)
        self.assertEqual(len(self.generator._order), len(self.generator._dataset))

    def test_len(self):
        self.assertEqual(len(self.generator), 4)

    def test_next(self):
        for i in range(10):
            batch = next(self.generator)
            self.assertEqual(len(batch), self.batch_size)

    def test_reset(self):
        self.generator.reset()
        self.assertEqual(self.generator._current_position, 0)
        self.assertEqual(len(self.generator._order), len(self.generator._dataset))

    def test_iter(self):
        self.assertEqual(self.generator.__iter__(), self.generator)


class TestSquadConverter(TestCase):
    def setUp(self):
        self.batch = [[
            'Rock n Roll is a risk. You risk being ridiculed.',
            'What is your risk?',
            38, 47,
            'ridiculed'
        ]]
        token_to_index = {'<pad>': 0, '<unk>': 1, 'is': 2, 'a': 3, 'risk': 4,
                          '.': 5, 'You': 6, 'being': 7, 'ridiculed': 8, 'What': 9,
                          'your': 10}
        self.converter = SquadConverter(token_to_index, 1, '<pad>', 3)
        self.token_to_index = token_to_index

    def test_init(self):
        self.assertEqual(self.converter._unk_index, 1)
        self.assertEqual(self.converter._pad_token, '<pad>')
        self.assertEqual(self.converter._categories, 3)
        self.assertEqual(self.converter._token_to_index, self.token_to_index)

    def test_call(self):
        import numpy as np

        inputs, output = self.converter(self.batch)
        question = np.array([[9, 2, 10, 4, 1]], dtype=np.int32)
        context = np.array([[1, 1, 1, 2, 3, 4, 5, 6, 4, 7, 8, 5]], dtype=np.int32)
        input_span = np.zeros(context.shape + (3,))
        input_span[0, :11, 0] = 1.
        input_span[0, 11, 1] = 1.
        output_span = np.array([[0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0]],
                               dtype=np.int32)[:, :, None]
        self.assertEqual(len(inputs), 3)
        np.testing.assert_array_equal(inputs[0], question)
        np.testing.assert_array_equal(inputs[1], context)
        np.testing.assert_array_equal(inputs[2], input_span)
        np.testing.assert_array_equal(output, output_span)

    def test_process_text(self):
        import numpy as np

        contexts = [self.converter._tokenizer(self.batch[0][0])]
        batch = self.converter._process_text(contexts)
        context = np.array([[1, 1, 1, 2, 3, 4, 5, 6, 4, 7, 8, 5]], dtype=np.int32)
        np.testing.assert_array_equal(batch, context)