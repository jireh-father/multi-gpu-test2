import glob
import os
import tensorflow as tf
from slim.preprocessing import preprocessing_factory
from tensorflow.contrib.data.python.ops import batching
from tensorflow.contrib.data.python.ops import threadpool


class Dataset(object):
    def __init__(self, sess, batch_size, shuffle, is_training, config, dataset_path, input_size):
        self.ds_handle_ph = tf.placeholder(tf.string, shape=[])
        self.sess = sess
        self.config = config
        self.input_size = input_size
        self.dataset_path = dataset_path
        self.training_handle = None
        self.validation_handle = None

        if is_training:
            conf_key = "train_name"
            dataset_map = self.train_dataset_map
        else:
            conf_key = "validation_name"
            dataset_map = self.test_dataset_map

        # files = tf.data.Dataset.list_files(dataset_path)
        files = tf.data.Dataset.list_files(
            os.path.join(config.dataset_dir, "%s_%s*tfrecord" % (config.dataset_name, getattr(config, conf_key))))

        # if hasattr(tf.contrib.data, "parallel_interleave"):
        #     ds = files.apply(tf.contrib.data.parallel_interleave(
        #         tf.data.TFRecordDataset, cycle_length=config.num_parallel_readers))
        # else:
        ds = files.interleave(tf.data.TFRecordDataset, cycle_length=config.num_parallel_readers)

        if config.cache_data:
            ds = ds.take(1).cache().repeat()

        counter = tf.data.Dataset.range(batch_size)
        counter = counter.repeat()
        ds = tf.data.Dataset.zip((ds, counter))
        ds = ds.prefetch(buffer_size=batch_size)
        # ds = ds.repeat()
        if shuffle:
            ds = ds.shuffle(buffer_size=config.buffer_size)
        if True:  # config.num_gpus > 1:
            batch_size_per_split = batch_size // config.num_gpus
            images = []
            labels = []

            ds = ds.apply(
                batching.map_and_batch(
                    map_func=dataset_map,
                    batch_size=batch_size_per_split,
                    num_parallel_batches=config.num_gpus))
            ds = ds.prefetch(buffer_size=config.num_gpus)
            # ds = ds.map(dataset_map, num_parallel_calls=batch_size)
            # ds = ds.batch(batch_size)
            # ds = ds.prefetch(buffer_size=batch_size)

            iterator = tf.data.Iterator.from_string_handle(
                self.ds_handle_ph, ds.output_types, ds.output_shapes)

            if config.datasets_num_private_threads:

                ds = threadpool.override_threadpool(
                    ds,
                    threadpool.PrivateThreadPool(
                        config.datasets_num_private_threads, display_name='input_pipeline_thread_pool'))
                self.training_iterator = ds.make_initializable_iterator()
                tf.add_to_collection(tf.GraphKeys.TABLE_INITIALIZERS,
                                     self.training_iterator.initializer)

            else:
                self.training_iterator = ds.make_one_shot_iterator()

            # self.training_iterator = ds.make_one_shot_iterator()
            for d in range(config.num_gpus):
                image, label = iterator.get_next()
                size = image.get_shape()[1]
                depth = image.get_shape()[3]
                image = tf.reshape(
                    image, shape=[batch_size_per_split, size, size, depth])
                label = tf.reshape(label, [batch_size_per_split, config.num_class])
                labels.append(label)
                images.append(image)
                # labels[d], images[d] = iterator.get_next()

            # for split_index in range(config.num_gpus):
            #     images[split_index] = tf.reshape(
            #         images[split_index],
            #         shape=[batch_size_per_split, config.input_size, config.input_size,
            #                config.num_channel])
            #     labels[split_index] = tf.reshape(labels[split_index],
            #                                      [batch_size_per_split])
            self.images = images
            self.labels = labels

        else:
            if hasattr(tf.contrib.data, "map_and_batch"):
                ds = ds.apply(tf.contrib.data.map_and_batch(map_func=dataset_map, batch_size=batch_size))
            else:
                ds = ds.map(map_func=dataset_map, num_parallel_calls=config.num_parallel_calls)
                ds = ds.batch(batch_size)
            ds = ds.prefetch(buffer_size=batch_size)

            self.iterator = ds.make_initializable_iterator()
            self.next_batch = self.iterator.get_next()

    def get_next_batch(self):
        return self.sess.run(self.next_batch)

    def init_multiple(self):
        self.training_handle = self.sess.run(self.training_iterator.string_handle())

        self.sess.run(self.training_iterator.initializer,
                      feed_dict={self.input_size: self.config.input_size})
        # self.validation_handle = self.sess.run(self.validation_iterator.string_handle())

    def init(self, dataset_path, input_size):
        self.sess.run(self.iterator.initializer,
                      feed_dict={self.dataset_path: dataset_path, self.input_size: input_size})

    def pre_process(self, example_proto, is_training):
        features = {"image/encoded": tf.FixedLenFeature((), tf.string, default_value=""),
                    "image/class/label": tf.FixedLenFeature((), tf.int64, default_value=0),
                    'image/height': tf.FixedLenFeature((), tf.int64, default_value=0),
                    'image/width': tf.FixedLenFeature((), tf.int64, default_value=0)
                    }

        parsed_features = tf.parse_single_example(example_proto, features)
        if self.config.preprocessing_name:
            image_preprocessing_fn = preprocessing_factory.get_preprocessing(self.config.preprocessing_name,
                                                                             is_training=is_training)
            image = tf.image.decode_image(parsed_features["image/encoded"], self.config.num_channel)
            image = tf.clip_by_value(
                image_preprocessing_fn(image, tf.convert_to_tensor(self.config.input_size),
                                       tf.convert_to_tensor(self.config.input_size)), -1, 1.0)
        else:
            image = tf.clip_by_value(tf.image.per_image_standardization(
                tf.image.resize_images(tf.image.decode_jpeg(parsed_features["image/encoded"], self.config.num_channel),
                                       [tf.convert_to_tensor(self.config.input_size),
                                        tf.convert_to_tensor(self.config.input_size)])), -1., 1.0)

        if len(parsed_features["image/class/label"].get_shape()) == 0:
            label = tf.one_hot(parsed_features["image/class/label"], self.config.num_class)
        else:
            label = parsed_features["image/class/label"]

        return image, label

    def train_dataset_map(self, example_proto, batch_position=0):
        return self.pre_process(example_proto, True)

    def test_dataset_map(self, example_proto, batch_position=0):
        return self.pre_process(example_proto, False)
