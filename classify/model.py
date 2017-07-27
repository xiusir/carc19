# Copyright 2015 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================

"""Builds the CARC-19 network.

Summary of available functions:

 # Compute input images and labels for training. If you would like to run
 # evaluations, use inputs() instead.
 inputs, labels = distorted_inputs()

 # Compute inference on the model inputs to make a prediction.
 predictions = inference(inputs)

 # Compute the total loss of the prediction with respect to the labels.
 loss = loss(predictions, labels)

 # Create a graph to run one step of training with respect to the loss.
 train_op = train(loss, global_step)
"""
# pylint: disable=missing-docstring
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os
import re
import sys
import tarfile

from six.moves import urllib
import tensorflow as tf

import carc_flags
import data_pipeline

FLAGS = tf.app.flags.FLAGS

# Basic model parameters.
# TODO tf_home must be changed to be your tensorflow working dir. Structure like:
# ex. ${tf_home}/tmp/carc34/{label_for_test.dat,label_for_train.dat,image/...}
# tf_home = /home/work/tensorflow
# mkdir -p /home/work/tensorflow/tmp
# mv small4w /home/work/tensorflow/tmp/carc34   
# OR mv big34w /home/work/tensorflow/tmp/carc34

# Global constants describing the CARC-19 data set.
IMAGE_SIZE = FLAGS.image_size
NUM_CLASSES = FLAGS.num_classes
NUM_EXAMPLES_PER_EPOCH_FOR_TRAIN = FLAGS.train_size
NUM_EXAMPLES_PER_EPOCH_FOR_EVAL = FLAGS.num_examples


# Constants describing the training process.
MOVING_AVERAGE_DECAY = FLAGS.moving_average_decay                   # The decay to use for the moving average.
NUM_EPOCHS_PER_DECAY = FLAGS.num_epochs_per_decay                   # Epochs after which learning rate decays.
LEARNING_RATE_DECAY_FACTOR = FLAGS.learning_rate_decay_factor       # Learning rate decay factor.
INITIAL_LEARNING_RATE = FLAGS.initial_learning_rate                 # Initial learning rate.

# If a model is trained with multiple GPUs, prefix all Op names with tower_name
# to differentiate the operations. Note that this prefix is removed from the
# names of the summaries when visualizing a model.
TOWER_NAME = 'tower'



def _activation_summary(x):
  """Helper to create summaries for activations.

  Creates a summary that provides a histogram of activations.
  Creates a summary that measures the sparsity of activations.

  Args:
    x: Tensor
  Returns:
    nothing
  """
  # Remove 'tower_[0-9]/' from the name in case this is a multi-GPU training
  # session. This helps the clarity of presentation on tensorboard.
  tensor_name = re.sub('%s_[0-9]*/' % TOWER_NAME, '', x.op.name)
  tf.summary.histogram(tensor_name + '/activations', x)
  tf.summary.scalar(tensor_name + '/sparsity',
                                       tf.nn.zero_fraction(x))


def _variable_on_cpu(name, shape, initializer, use_cpu=True):
  """Helper to create a Variable stored on CPU memory.

  Args:
    name: name of the variable
    shape: list of ints
    initializer: initializer for Variable

  Returns:
    Variable Tensor
  """
  if use_cpu:
    with tf.device('/cpu:0'):
      dtype = tf.float16 if FLAGS.use_fp16 else tf.float32
      #var = tf.get_variable(name, shape, initializer=initializer, dtype=dtype)
      var = tf.Variable(initializer(shape, dtype=dtype), name=name)
  else:
    dtype = tf.float16 if FLAGS.use_fp16 else tf.float32
    var = tf.Variable(initializer(shape, dtype=dtype), name=name)

  return var


def _variable_with_weight_decay(name, shape, stddev, wd):
  """Helper to create an initialized Variable with weight decay.

  Note that the Variable is initialized with a truncated normal distribution.
  A weight decay is added only if one is specified.

  Args:
    name: name of the variable
    shape: list of ints
    stddev: standard deviation of a truncated Gaussian
    wd: add L2Loss weight decay multiplied by this float. If None, weight
        decay is not added for this Variable.

  Returns:
    Variable Tensor
  """
  dtype = tf.float16 if FLAGS.use_fp16 else tf.float32
  var = _variable_on_cpu(
      name,
      shape,
      tf.truncated_normal_initializer(stddev=stddev, dtype=dtype))
  if wd is not None:
    weight_decay = tf.multiply(tf.nn.l2_loss(var), wd, name='weight_loss')
    tf.add_to_collection('losses', weight_decay)
  return var


def train_inputs():
  """Construct distorted input for CARC training using the Reader ops.

  Returns:
    images: Images. 4D tensor of [batch_size, IMAGE_SIZE, IMAGE_SIZE, 3] size.
    labels: Labels. 1D tensor of [batch_size] size.

  Raises:
    ValueError: If no data_dir
  """
  if not FLAGS.data_dir:
    raise ValueError('Please supply a data_dir')
  data_dir = FLAGS.data_dir
  images, labels, _ = data_pipeline.train_inputs(data_dir=data_dir,
                                             batch_size=FLAGS.batch_size)
  if FLAGS.use_fp16:
    images = tf.cast(images, tf.float16)
    labels = tf.cast(labels, tf.float16)
  return images, labels


def evaluate_inputs(eval_data):
  """Construct input for CARC evaluation using the Reader ops.

  Args:
    eval_data: bool, indicating if one should use the train or eval data set.

  Returns:
    images: Images. 4D tensor of [batch_size, IMAGE_SIZE, IMAGE_SIZE, 3] size.
    labels: Labels. 1D tensor of [batch_size] size.

  Raises:
    ValueError: If no data_dir
  """
  if not FLAGS.data_dir:
    raise ValueError('Please supply a data_dir')
  data_dir = FLAGS.data_dir
  images, labels, keys = data_pipeline.evaluate_inputs(eval_data=eval_data,
                                        data_dir=data_dir,
                                        batch_size=FLAGS.batch_size)
  if FLAGS.use_fp16:
    images = tf.cast(images, tf.float16)
    labels = tf.cast(labels, tf.float16)
  return images, labels, keys


def inference(images):
  """Build the CARC-19 model.

  Args:
    images: Images returned from distorted_inputs() or inputs().

  Returns:
    Logits.
  """
  # We instantiate all variables using tf.get_variable() instead of
  # tf.Variable() in order to share variables across multiple GPU training runs.
  # If we only ran this model on a single GPU, we could simplify this function
  # by replacing all instances of tf.get_variable() with tf.Variable().
  #
  #float_image = tf.image.per_image_standardization(distorted_image)
  images = tf.identity(images, 'input')
  images = tf.reshape(images, [-1, 256, 256, 3])
  #images = tf.map_fn(lambda img: tf.image.per_image_standardization(img), images)

  # conv1
  with tf.variable_scope('conv1') as scope:
    kernel = _variable_with_weight_decay('weights',
                                         shape=[11, 11, 3, 32],
                                         stddev=5e-2,
                                         wd=0.0)
    conv = tf.nn.conv2d(images, kernel, [1, 1, 1, 1], padding='SAME')
    biases = _variable_on_cpu('biases', [32], tf.constant_initializer(0.0))
    pre_activation = tf.nn.bias_add(conv, biases)
    conv1 = tf.nn.relu(pre_activation, name=scope.name)
    _activation_summary(conv1)

  # pool1
  pool1 = tf.nn.max_pool(conv1, ksize=[1, 3, 3, 1], strides=[1, 4, 4, 1],
                         padding='SAME', name='pool1')
  # norm1
  norm1 = tf.nn.lrn(pool1, 4, bias=1.0, alpha=0.001 / 9.0, beta=0.75,
                    name='norm1')

  # conv2
  with tf.variable_scope('conv2') as scope:
    kernel = _variable_with_weight_decay('weights',
                                         shape=[5, 5, 32, 96],
                                         stddev=5e-2,
                                         wd=0.0)
    conv = tf.nn.conv2d(norm1, kernel, [1, 1, 1, 1], padding='SAME')
    biases = _variable_on_cpu('biases', [96], tf.constant_initializer(0.1))
    pre_activation = tf.nn.bias_add(conv, biases)
    conv2 = tf.nn.relu(pre_activation, name=scope.name)
    _activation_summary(conv2)

  # norm2
  norm2 = tf.nn.lrn(conv2, 4, bias=1.0, alpha=0.001 / 9.0, beta=0.75,
                    name='norm2')
  # pool2
  pool2 = tf.nn.max_pool(norm2, ksize=[1, 3, 3, 1],
                         strides=[1, 2, 2, 1], padding='SAME', name='pool2')

  # conv3
  with tf.variable_scope('conv3') as scope:
    kernel = _variable_with_weight_decay('weights',
                                         shape=[3, 3, 96, 192],
                                         stddev=5e-2,
                                         wd=0.0)
    conv = tf.nn.conv2d(pool2, kernel, [1, 1, 1, 1], padding='SAME')
    biases = _variable_on_cpu('biases', [192], tf.constant_initializer(0.1))
    pre_activation = tf.nn.bias_add(conv, biases)
    conv3 = tf.nn.relu(pre_activation, name=scope.name)
    _activation_summary(conv3)
  #### norm3
  ###norm3 = tf.nn.lrn(conv3, 4, bias=1.0, alpha=0.001 / 9.0, beta=0.75,
  ###                  name='norm3')
  #### pool3
  ###pool3 = tf.nn.max_pool(norm3, ksize=[1, 3, 3, 1],
  ###                       strides=[1, 2, 2, 1], padding='SAME', name='pool3')

  #### conv4
  ###with tf.variable_scope('conv4') as scope:
  ###  kernel = _variable_with_weight_decay('weights',
  ###                                       shape=[3, 3, 192, 256],
  ###                                       stddev=5e-2,
  ###                                       wd=0.0)
  ###  conv = tf.nn.conv2d(layer3, kernel, [1, 1, 1, 1], padding='SAME')
  ###  biases = _variable_on_cpu('biases', [256], tf.constant_initializer(0.1))
  ###  pre_activation = tf.nn.bias_add(conv, biases)
  ###  conv4 = tf.nn.relu(pre_activation, name=scope.name)
  ###  _activation_summary(conv4)
  #### norm4
  ###norm4 = tf.nn.lrn(conv4, 4, bias=1.0, alpha=0.001 / 9.0, beta=0.75,
  ###                  name='norm4')
  #### pool4
  ###pool4 = tf.nn.max_pool(norm4, ksize=[1, 3, 3, 1],
  ###                       strides=[1, 2, 2, 1], padding='SAME', name='pool3')

  # conv5
  with tf.variable_scope('conv5') as scope:
    kernel = _variable_with_weight_decay('weights',
                                         shape=[3, 3, 192, 128],
                                         stddev=5e-2,
                                         wd=0.0)
    conv = tf.nn.conv2d(conv3, kernel, [1, 1, 1, 1], padding='SAME')
    biases = _variable_on_cpu('biases', [128], tf.constant_initializer(0.1))
    pre_activation = tf.nn.bias_add(conv, biases)
    conv5 = tf.nn.relu(pre_activation, name=scope.name)
    _activation_summary(conv5)
  # norm5
  norm5 = tf.nn.lrn(conv5, 4, bias=1.0, alpha=0.001 / 9.0, beta=0.75,
                    name='norm5')
  # pool5
  pool5 = tf.nn.max_pool(norm5, ksize=[1, 3, 3, 1],
                         strides=[1, 2, 2, 1], padding='SAME', name='pool3')

  # local6
  with tf.variable_scope('local6') as scope:
    # Move everything into depth so we can perform a single matrix multiply.
    ##reshape = tf.reshape(pool5, [FLAGS.batch_size, -1])
    ##dim = reshape.get_shape()[1].value
    ##weights = _variable_with_weight_decay('weights', shape=[dim, 1024],
    dim0 = pool5.get_shape()[0].value
    dim1 = pool5.get_shape()[1].value
    dim2 = pool5.get_shape()[2].value
    dim3 = pool5.get_shape()[3].value
    reshape = tf.reshape(pool5, [-1, dim1*dim2*dim3])
    dim_in = dim1*dim2*dim3
    weights = _variable_with_weight_decay('weights', shape=[dim_in, 1024],
                                          stddev=0.04, wd=0.004)
    biases = _variable_on_cpu('biases', [1024], tf.constant_initializer(0.1))
    local6 = tf.nn.relu(tf.matmul(reshape, weights) + biases, name=scope.name)
    _activation_summary(local6)

  #### local7
  ###with tf.variable_scope('local7') as scope:
  ###  weights = _variable_with_weight_decay('weights', shape=[1024, 512],
  ###                                        stddev=0.04, wd=0.004)
  ###  biases = _variable_on_cpu('biases', [512], tf.constant_initializer(0.1))
  ###  local7 = tf.nn.relu(tf.matmul(layer6, weights) + biases, name=scope.name)
  ###  _activation_summary(local7)

  # linear layer(WX + b),
  # We don't apply softmax here because
  # tf.nn.sparse_softmax_cross_entropy_with_logits accepts the unscaled logits
  # and performs the softmax internally for efficiency.
  with tf.variable_scope('softmax_linear') as scope:
    weights = _variable_with_weight_decay('weights', [1024, NUM_CLASSES],
                                          stddev=1/1024.0, wd=0.0)
    biases = _variable_on_cpu('biases', [NUM_CLASSES],
                              tf.constant_initializer(0.0))
    softmax_linear = tf.add(tf.matmul(local6, weights), biases, name=scope.name)
    _activation_summary(softmax_linear)

  softmax_linear = tf.identity(softmax_linear, 'predict')
  softmax_linear = tf.identity(softmax_linear, 'output')
  return softmax_linear


def loss(logits, labels):
  """Add L2Loss to all the trainable variables.

  Add summary for "Loss" and "Loss/avg".
  Args:
    logits: Logits from inference().
    labels: Labels from distorted_inputs or inputs(). 1-D tensor
            of shape [batch_size]

  Returns:
    Loss tensor of type float.
  """
  # Calculate the average cross entropy loss across the batch.
  labels = tf.cast(labels, tf.int64)
  cross_entropy = tf.nn.sparse_softmax_cross_entropy_with_logits(
      labels=labels, logits=logits, name='cross_entropy_per_example')
  cross_entropy_mean = tf.reduce_mean(cross_entropy, name='cross_entropy')
  tf.add_to_collection('losses', cross_entropy_mean)

  # The total loss is defined as the cross entropy loss plus all of the weight
  # decay terms (L2 loss).
  return tf.add_n(tf.get_collection('losses'), name='total_loss')


def _add_loss_summaries(total_loss):
  """Add summaries for losses in CARC-19 model.

  Generates moving average for all losses and associated summaries for
  visualizing the performance of the network.

  Args:
    total_loss: Total loss from loss().
  Returns:
    loss_averages_op: op for generating moving averages of losses.
  """
  # Compute the moving average of all individual losses and the total loss.
  loss_averages = tf.train.ExponentialMovingAverage(0.9, name='avg')
  losses = tf.get_collection('losses')
  loss_averages_op = loss_averages.apply(losses + [total_loss])

  # Attach a scalar summary to all individual losses and the total loss; do the
  # same for the averaged version of the losses.
  for l in losses + [total_loss]:
    # Name each loss as '(raw)' and name the moving average version of the loss
    # as the original loss name.
    tf.summary.scalar(l.op.name + ' (raw)', l)
    tf.summary.scalar(l.op.name, loss_averages.average(l))

  return loss_averages_op


def train(total_loss, global_step):
  """Train CARC-19 model.

  Create an optimizer and apply to all trainable variables. Add moving
  average for all trainable variables.

  Args:
    total_loss: Total loss from loss().
    global_step: Integer Variable counting the number of training steps
      processed.
  Returns:
    train_op: op for training.
  """
  # Variables that affect learning rate.
  num_batches_per_epoch = NUM_EXAMPLES_PER_EPOCH_FOR_TRAIN / FLAGS.batch_size
  decay_steps = int(num_batches_per_epoch * NUM_EPOCHS_PER_DECAY)

  # Decay the learning rate exponentially based on the number of steps.
  lr = tf.train.exponential_decay(INITIAL_LEARNING_RATE,
                                  global_step,
                                  decay_steps,
                                  LEARNING_RATE_DECAY_FACTOR,
                                  staircase=True)
  tf.summary.scalar('learning_rate', lr)

  # Generate moving averages of all losses and associated summaries.
  loss_averages_op = _add_loss_summaries(total_loss)

  # Compute gradients.
  with tf.control_dependencies([loss_averages_op]):
    opt = tf.train.GradientDescentOptimizer(lr)
    grads = opt.compute_gradients(total_loss)

  # Apply gradients.
  apply_gradient_op = opt.apply_gradients(grads, global_step=global_step)

  # Add histograms for trainable variables.
  for var in tf.trainable_variables():
    tf.summary.histogram(var.op.name, var)

  # Add histograms for gradients.
  for grad, var in grads:
    if grad is not None:
      tf.summary.histogram(var.op.name + '/gradients', grad)

  # Track the moving averages of all trainable variables.
  variable_averages = tf.train.ExponentialMovingAverage(
      MOVING_AVERAGE_DECAY, global_step)
  variables_averages_op = variable_averages.apply(tf.trainable_variables())

  with tf.control_dependencies([apply_gradient_op, variables_averages_op]):
    train_op = tf.no_op(name='train')

  return train_op


def maybe_download_and_extract():
  """Download and extract the tarball from Alex's website."""
  pass
  ##DATA_URL = 'http://www.cs.toronto.edu/~kriz/carc-19-binary.tar.gz'
  ##dest_directory = FLAGS.data_dir
  ##if not os.path.exists(dest_directory):
  ##  os.makedirs(dest_directory)
  ##filename = DATA_URL.split('/')[-1]
  ##filepath = os.path.join(dest_directory, filename)
  ##if not os.path.exists(filepath):
  ##  def _progress(count, block_size, total_size):
  ##    sys.stdout.write('\r>> Downloading %s %.1f%%' % (filename,
  ##        float(count * block_size) / float(total_size) * 100.0))
  ##    sys.stdout.flush()
  ##  filepath, _ = urllib.request.urlretrieve(DATA_URL, filepath, _progress)
  ##  print()
  ##  statinfo = os.stat(filepath)
  ##  print('Successfully downloaded', filename, statinfo.st_size, 'bytes.')
  ##extracted_dir_path = os.path.join(dest_directory, 'carc-19-batches-bin')
  ##if not os.path.exists(extracted_dir_path):
  ##  tarfile.open(filepath, 'r:gz').extractall(dest_directory)