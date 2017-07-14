"""Script for training DCSP for segmentation task.
"""

from __future__ import print_function

import argparse
from datetime import datetime
import os
import sys
import time

import tensorflow as tf
import numpy as np

from deeplab_resnet import DeepLabResNetModel, ImageReader_Segment, decode_labels, inv_preprocess, prepare_label

NUM_CLASSES = 21

BATCH_SIZE = 10
DATA_DIRECTORY = '/home/VOCdevkit'
DATA_LIST_PATH = './dataset/train.txt'
INPUT_SIZE = '321,321'
LEARNING_RATE = 1e-3
MOMENTUM = 0.9
NUM_STEPS = 2001
START_STEP = 0
POWER = 0.9
RESTORE_FROM = './deeplab_resnet.ckpt'
SAVE_NUM_IMAGES = 2
SAVE_PRED_EVERY = 1000
SNAPSHOT_DIR = './snapshots/'
WEIGHT_DECAY = 0.0005

EPSILON = 1e-12
GLOBAL_STEP = 0
RANDOM_SEED = 1234
RANDOM_SCALE = True
RANDOM_MIRROR = True

def save(saver, sess, logdir, step):
   '''Save weights.
   
   Args:
     saver: TensorFlow Saver object.
     sess: TensorFlow session.
     logdir: path to the snapshots directory.
     step: current training step.
   '''
   model_name = 'model.ckpt'
   checkpoint_path = os.path.join(logdir, model_name)
    
   if not os.path.exists(logdir):
      os.makedirs(logdir)
   saver.save(sess, checkpoint_path, global_step=step)
   print('The checkpoint has been created.')

def load(saver, sess, ckpt_path):
    '''Load trained weights.
    
    Args:
      saver: TensorFlow Saver object.
      sess: TensorFlow session.
      ckpt_path: path to checkpoint file with parameters.
    ''' 
    saver.restore(sess, ckpt_path)
    print("Restored model parameters from {}".format(ckpt_path))

def main(data_dir=DATA_DIRECTORY, data_list=DATA_LIST_PATH, start_step=START_STEP, num_steps=NUM_STEPS,\
         global_step=GLOBAL_STEP, restore_from=RESTORE_FROM, snapshot_dir=SNAPSHOT_DIR,\
         base_learning_rate=LEARNING_RATE, n_classes=NUM_CLASSES, adapt=False, input_size=(321,321)):
    """Create the model and start the training."""
    
    graph = tf.Graph()

    with graph.as_default():

        tf.set_random_seed(RANDOM_SEED)
    
        # Create queue coordinator.
        coord = tf.train.Coordinator()
    
        # Load reader for training.
        with tf.name_scope("create_inputs"):
            reader = ImageReader_Segment(
                data_dir,
                data_list,
                input_size,
                RANDOM_SEED,
                RANDOM_SCALE,
                RANDOM_MIRROR,
                n_classes,
                adapt,
                coord)
            image_batch, label_batch, catg_batch = reader.dequeue(BATCH_SIZE)

        # Create network.
        net = DeepLabResNetModel({'data': image_batch}, is_training=False)

        # For a small batch size, it is better to keep 
        # the statistics of the BN layers (running means and variances)
        # frozen, and to not update the values provided by the pre-trained model. 
        # If is_training=True, the statistics will be updated during the training.
        # Note that is_training=False still updates BN parameters gamma (scale) and beta (offset)
        # if they are presented in var_list of the optimiser definition.

        # Predictions.
        raw_output_seg = net.layers['fc1_voc12']
        raw_output_classfc = net.layers['fc1_voc12_d0']
        # Which variables to load. Running means and variances are not trainable,
        # thus all_variables() should be restored.
        restore_var = tf.global_variables()
        all_trainable = [v for v in tf.trainable_variables() if 'beta' not in v.name and 'gamma' not in v.name]
        fc_trainable = [v for v in all_trainable if 'fc' in v.name]
        fc_d_trainable = [v for v in fc_trainable if '_d0' in v.name]
        conv_trainable = [v for v in all_trainable if 'fc' not in v.name] # lr * 1.0
        fc_c_w_trainable = [v for v in fc_trainable if 'weights' in v.name and '_d0' not in v.name] # lr * 10.0
        fc_c_b_trainable = [v for v in fc_trainable if 'biases' in v.name and '_d0' not in v.name] # lr * 20.0
        fc_d_w_trainable = [v for v in fc_d_trainable if 'weights' in v.name] # lr * 10.0
        fc_d_b_trainable = [v for v in fc_d_trainable if 'biases' in v.name] # lr * 20.0
        assert(len(all_trainable) == len(fc_trainable) + len(conv_trainable))
        assert(len(fc_trainable) == len(fc_c_w_trainable) + len(fc_c_b_trainable) +\
               len(fc_d_w_trainable) + len(fc_d_b_trainable))

        # Add histogram of all variables
        for v in conv_trainable + fc_trainable:
            tf.summary.histogram(v.name.replace(":", "_"), v)
   
        # Do the global average pooling
        g_avg_pool = tf.reduce_mean(tf.reduce_mean(raw_output_classfc, axis=1, keep_dims=True),\
                               axis=2, keep_dims=True) # Avg across the width and height dimension -> [Bx1x1x20]
        g_avg_pool_sqzd = tf.squeeze(g_avg_pool, axis=[1, 2])

        # Resize the label batch to the size of predictions
        label_proc = tf.image.resize_nearest_neighbor(label_batch, tf.stack(raw_output_seg.get_shape()[1:3]))
        label_proc = tf.squeeze(label_proc, axis=3)

        # Change the shapes of ground truth and predictions
        raw_gt = tf.reshape(label_proc, [-1,])
        raw_prediction = tf.reshape(raw_output_seg, [-1, n_classes])
                                                  
        # Classification loss
        classfc_loss = tf.reduce_mean(tf.nn.sigmoid_cross_entropy_with_logits(logits=g_avg_pool_sqzd, labels=catg_batch))

        # Pixel-wise softmax loss.
        seg_loss = tf.reduce_mean(tf.nn.sparse_softmax_cross_entropy_with_logits(logits=raw_prediction, labels=raw_gt))

        # L2 loss
        l2_losses = [WEIGHT_DECAY * tf.nn.l2_loss(v) for v in tf.trainable_variables() if 'weights' in v.name]

        # L2 and classification loss
        l2_classfc_loss = tf.add_n(l2_losses) + classfc_loss

        # Combined loss
        reduced_loss = seg_loss + l2_classfc_loss

        # Add loss to summary
        tf.summary.scalar("loss", reduced_loss)

        # Processed predictions: for visualisation.
        raw_output_up = tf.image.resize_bilinear(raw_output_seg, tf.shape(image_batch)[1:3,])
        raw_output_up = tf.argmax(raw_output_up, dimension=3)
        pred = tf.expand_dims(raw_output_up, dim=3)

        # Image summary.
        images_summary = tf.py_func(inv_preprocess, [image_batch, SAVE_NUM_IMAGES], tf.uint8)
        preds_summary = tf.py_func(decode_labels, [pred, SAVE_NUM_IMAGES], tf.uint8)
    
        total_summary = tf.summary.image('images', 
                                         tf.concat(axis=2, values=[images_summary, preds_summary]), 
                                         max_outputs=SAVE_NUM_IMAGES) # Concatenate row-wise.
        merged_summary = tf.summary.merge_all()
        summary_writer = tf.summary.FileWriter(snapshot_dir,
                                               graph=graph)
   
        # Define loss and optimisation parameters.
        base_lr = tf.constant(base_learning_rate)
        step_ph = tf.placeholder(dtype=tf.float32, shape=())
        learning_rate = tf.scalar_mul(base_lr, tf.pow((1 - step_ph / num_steps), POWER))
    
        opt_conv = tf.train.MomentumOptimizer(learning_rate, MOMENTUM)
        opt_fc_c_w = tf.train.MomentumOptimizer(learning_rate * 10.0, MOMENTUM)
        opt_fc_c_b = tf.train.MomentumOptimizer(learning_rate * 20.0, MOMENTUM) # reducing the learning rate
        opt_fc_d_w = tf.train.MomentumOptimizer(learning_rate * 10.0, MOMENTUM)
        opt_fc_d_b = tf.train.MomentumOptimizer(learning_rate * 20.0, MOMENTUM) # reducing the learning rate

        grads_pixel_loss = tf.gradients(seg_loss, conv_trainable + fc_c_w_trainable + fc_c_b_trainable)
        grads_l2_classfc_loss = tf.gradients(l2_classfc_loss, conv_trainable + fc_c_w_trainable \
                + fc_c_b_trainable + fc_d_w_trainable + fc_d_b_trainable)

        grads_conv = grads_pixel_loss[:len(conv_trainable)] + grads_l2_classfc_loss[:len(conv_trainable)]

        grads_fc_c_w = grads_pixel_loss[len(conv_trainable) : (len(conv_trainable) + len(fc_c_w_trainable))] \
                + grads_l2_classfc_loss[len(conv_trainable) : (len(conv_trainable) + len(fc_c_w_trainable))]

        grads_fc_c_b = grads_pixel_loss[(len(conv_trainable) + len(fc_c_w_trainable)):] \
                + grads_l2_classfc_loss[(len(conv_trainable) + len(fc_c_w_trainable)) : \
                (len(conv_trainable) + len(fc_c_w_trainable) + len(fc_c_b_trainable))]

        grads_fc_d_w = grads_l2_classfc_loss[(len(conv_trainable) + len(fc_c_w_trainable) + len(fc_c_b_trainable)) : \
                (len(conv_trainable) + len(fc_c_w_trainable) + len(fc_c_b_trainable) + \
                len(fc_d_w_trainable))]

        grads_fc_d_b = grads_l2_classfc_loss[(len(conv_trainable) + len(fc_c_w_trainable) + len(fc_c_b_trainable) + \
                len(fc_d_w_trainable)):]

        train_op_conv = opt_conv.apply_gradients(zip(grads_conv, conv_trainable))
        train_op_fc_c_w = opt_fc_c_w.apply_gradients(zip(grads_fc_c_w, fc_c_w_trainable))
        train_op_fc_c_b = opt_fc_c_b.apply_gradients(zip(grads_fc_c_b, fc_c_b_trainable))
        train_op_fc_d_w = opt_fc_d_w.apply_gradients(zip(grads_fc_d_w, fc_d_w_trainable))
        train_op_fc_d_b = opt_fc_d_b.apply_gradients(zip(grads_fc_d_b, fc_d_b_trainable))

        train_op = tf.group(train_op_conv, train_op_fc_c_w, train_op_fc_c_b, train_op_fc_d_w, train_op_fc_d_b)
 
    # Set up tf session and initialize variables. 
    config = tf.ConfigProto()
    config.gpu_options.allow_growth = True

    with tf.Session(config=config, graph=graph) as sess:

        # Initialize the model parameters
        tf.global_variables_initializer().run()

        # Saver for storing checkpoints of the model.
        saver = tf.train.Saver(var_list=restore_var, max_to_keep=10)
    
        # Load variables if the checkpoint is provided.
        if restore_from is not None:
            loader = tf.train.Saver(var_list=restore_var)
            load(loader, sess, restore_from)
    
        # Start queue threads.
        threads = tf.train.start_queue_runners(coord=coord, sess=sess)

        # Iterate over training steps.
        for step in range(start_step+1, num_steps):
            start_time = time.time()
            feed_dict = {step_ph:step}
        
            if step % SAVE_PRED_EVERY == 0:
                loss_value, summary, _ = sess.run([reduced_loss, merged_summary, train_op], feed_dict=feed_dict)
                summary_writer.add_summary(summary, (step + global_step))
                save(saver, sess, snapshot_dir, (step + global_step))
            else:
                loss_value, lr, _ = sess.run([reduced_loss, learning_rate, train_op], feed_dict=feed_dict)
            duration = time.time() - start_time
            print('step {:d} global_step {:d} \t loss = {:.3f} lr = {:.5f} ({:.3f} sec/step)'.\
                  format(step, step + global_step, loss_value, lr, duration))

        coord.request_stop()
        coord.join(threads)
    
if __name__ == '__main__':
    main()
