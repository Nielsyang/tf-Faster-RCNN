# -*- coding: utf-8 -*-
"""
Created on Sat Dec 31 13:22:36 2016

@author: Kevin Liang

Faster R-CNN model using a simple 5-layer conv net as the convolutional feature extractor

Reorganizing a few things relative to faster_rcnn_conv5
"""

import sys

sys.path.append('../')

from Lib.TensorBase.tensorbase.base import Model, Data
from Lib.test_aux import test_net, vis_detections

from Networks.convnet import convnet
from Networks.faster_rcnn_networks_mnist import rpn, roi_proposal, fast_rcnn

from tqdm import tqdm

import numpy as np
import tensorflow as tf
import argparse

# Global Dictionary of Flags
flags = {
    'data_directory': '../Data/data_trans/',  # Location of training/testing files
    'save_directory': '../Logs/',  # Where to create model_directory folder
    'model_directory': 'conv5/',  # Where to create 'Model[n]' folder
    'batch_size': 1,
    'display_step': 500,  # How often to display loss
    'num_classes': 11,  # 10 digits, +1 for background
    'classes': ('__background__', '1', '2', '3', '4', '5', '6', '7', '8', '9', '0'),
    'anchor_scales': [1, 2, 3]
}


class FasterRcnnConv5(Model):
    def __init__(self, flags_input):
        super().__init__(flags_input, flags_input['run_num'], vram=0.2, restore=flags_input['restore_num'])
        self.print_log("Seed: %d" % flags_input['seed'])
        self.threads, self.coord = Data.init_threads(self.sess)

    def _data(self):
        # Initialize placeholder dicts
        self.x = {}
        self.gt_boxes = {}
        self.im_dims = {}

        # Train data
        file_train = flags['data_directory'] + 'trans_mnist_train.tfrecords'
        self.x['TRAIN'], self.gt_boxes['TRAIN'], self.im_dims['TRAIN'] = Data.batch_inputs(self.read_and_decode,
                                                                                           file_train, batch_size=self.flags['batch_size'])
        # Validation data; ground truth boxes used for evaluation/visualization only
        file_valid = flags['data_directory'] + 'trans_mnist_valid.tfrecords'
        self.x['VALID'], self.gt_boxes['VALID'], self.im_dims['VALID'] = Data.batch_inputs(self.read_and_decode,
                                                                                          file_valid, mode="eval",
                                                                                          batch_size=self.flags['batch_size'],
                                                                                          num_threads=1, num_readers=1)
        # Test data. No GT Boxes.
        self.x['TEST'] = tf.placeholder(tf.float32, [None, 128, 128, 1])
        self.im_dims['TEST'] = tf.placeholder(tf.int32, [None, 2])

        self.num_images = {'TRAIN': 55000, 'VALID': 5000, 'TEST': 10000}

    def _summaries(self):
        """ Define summaries for TensorBoard """
        tf.summary.scalar("Total_Loss", self.cost)
        tf.summary.scalar("RPN_cls_Loss", self.rpn_cls_loss)
        tf.summary.scalar("RPN_bbox_Loss", self.rpn_bbox_loss)
        tf.summary.scalar("Fast_RCNN_Cls_Loss", self.fast_rcnn_cls_loss)
        tf.summary.scalar("Fast_RCNN_Bbox_Loss", self.fast_rcnn_bbox_loss)
        tf.summary.image("x_train", self.x['TRAIN'])

    def _network(self):
        """ Define the network outputs """
        # Initialize network dicts
        self.cnn = {}
        self.rpn_net = {}
        self.roi_proposal_net = {}
        self.fast_rcnn_net = {}

        # Train network
        with tf.variable_scope('model'):
            self._faster_rcnn(self.x['TRAIN'], self.gt_boxes['TRAIN'], self.im_dims['TRAIN'], 'TRAIN')

        # Valid network => Uses same weights as train network
        with tf.variable_scope('model', reuse=True):
            assert tf.get_variable_scope().reuse is True
            self._faster_rcnn(self.x['VALID'], None, self.im_dims['VALID'], 'VALID')

        # Test network => Uses same weights as train network
        with tf.variable_scope('model', reuse=True):
            assert tf.get_variable_scope().reuse is True
            self._faster_rcnn(self.x['TEST'], None, self.im_dims['TEST'], 'TEST')

    def _faster_rcnn(self, x, gt_boxes, im_dims, key):
        # VALID and TEST are both evaluation mode
        eval_mode = True if (key == 'VALID' or key == 'TEST') else False

        self.cnn[key] = convnet(x, [5, 3, 3, 3, 3], [32, 64, 64, 128, 128], strides=[2, 2, 1, 2, 1])
        featureMaps = self.cnn[key].get_output()
        _feat_stride = self.cnn[key].get_feat_stride()

        # Region Proposal Network (RPN)
        self.rpn_net[key] = rpn(featureMaps, gt_boxes, im_dims, _feat_stride, eval_mode, flags)

        # Roi Pooling
        self.roi_proposal_net[key] = roi_proposal(self.rpn_net[key], gt_boxes, im_dims, eval_mode, flags)

        # R-CNN Classification
        self.fast_rcnn_net[key] = fast_rcnn(featureMaps, self.roi_proposal_net[key], eval_mode)

    def _optimizer(self):
        """ Define losses and initialize optimizer """
        # Losses (come from TRAIN networks)
        self.rpn_cls_loss = self.rpn_net['TRAIN'].get_rpn_cls_loss()
        self.rpn_bbox_loss = self.rpn_net['TRAIN'].get_rpn_bbox_loss()
        self.fast_rcnn_cls_loss = self.fast_rcnn_net['TRAIN'].get_fast_rcnn_cls_loss()
        self.fast_rcnn_bbox_loss = self.fast_rcnn_net['TRAIN'].get_fast_rcnn_bbox_loss()

        # Total Loss (Note: Fast R-CNN bbox refinement loss disabled)
        self.cost = tf.reduce_sum(self.rpn_cls_loss + self.rpn_bbox_loss + self.fast_rcnn_cls_loss)

        # Optimization operation
        self.optimizer = tf.train.AdamOptimizer().minimize(self.cost)

    def _run_train_iter(self):
        """ Run training iteration"""
        summary, _ = self.sess.run([self.merged, self.optimizer])
        return summary

    def _record_train_metrics(self):
        """ Record training metrics """
        loss = self.sess.run(self.cost)
        self.print_log('Step %d: loss = %.6f' % (self.step, loss))

    def train(self):
        """ Run training function. Save model upon completion """
        epochs = 0
        iterations = int(np.ceil(self.num_images['TRAIN'] / self.flags['batch_size']) * self.flags['num_epochs'])
        self.print_log('Training for %d iterations' % iterations)
        self.step += 1
        for i in tqdm(range(iterations)):
            summary = self._run_train_iter()
            self._record_training_step(summary)
            if self.step % (self.flags['display_step']) == 0:
                self._record_train_metrics()
            if self.step % (self.num_images['TRAIN']) == 0:  # save model every 1 epoch
                epochs += 1
                if self.step % (self.num_images['TRAIN'] * 1) == 0:
                    self._save_model(section=epochs) 

    def test(self):
        """ Evaluate network on the test set. """
        data_info = (self.num_images['TEST'], flags['num_classes'], flags['classes'])

        tf_inputs = (self.x['TEST'], self.im_dims['TEST'])
        tf_outputs = (self.roi_proposal_net['TEST'].get_rois(),
                      self.fast_rcnn_net['TEST'].get_cls_prob(),
                      self.fast_rcnn_net['TEST'].get_bbox_refinement())

        class_metrics = test_net(self.sess, flags['data_directory'], data_info, tf_inputs, tf_outputs)
        print(class_metrics)
        
    def test_print_image(self):
        """ Read data through self.sess and plot out """
        print("Running 100 iterations of simple data transfer from queue to np.array")
        for i in range(100):
            bboxes, cls_score, gt_boxes, image = self.sess.run([self.roi_proposal_net['VALID'].get_rois(),
                                                                self.fast_rcnn_net['VALID'].get_cls_prob(),
                                                                self.gt_boxes['VALID'], self.x['VALID']])
            cls = np.argmax(cls_score, 1)
            print(cls_score.shape)
            print(cls)
            vis_detections(np.squeeze(image[0]), gt_boxes, bboxes, cls)
            print('Image Num: %d' % i)

    def close(self):
        Data.exit_threads(self.threads, self.coord)

    @staticmethod
    def read_and_decode(example_serialized):
        """ Read and decode binarized, raw MNIST dataset from .tfrecords file generated by MNIST.py """
        features = tf.parse_single_example(
            example_serialized,
            features={
                'image': tf.FixedLenFeature([], tf.string),
                'gt_boxes': tf.FixedLenFeature([5], tf.int64, default_value=[-1] * 5),  # 10 classes in MNIST
                'dims': tf.FixedLenFeature([2], tf.int64, default_value=[-1] * 2)
            })
        # now return the converted data
        gt_boxes = features['gt_boxes']
        dims = features['dims']
        image = tf.decode_raw(features['image'], tf.float32)
        image = tf.reshape(image, [128, 128, 1])
        return image, tf.cast(gt_boxes, tf.int32), tf.cast(dims, tf.int32)


def main():
    flags['seed'] = 1234

    # Parse Arguments
    parser = argparse.ArgumentParser(description='Bayesian Ladder Networks Arguments')
    parser.add_argument('-n', '--run_num', default=0)  # Saves all under /save_directory/model_directory/Model[n]
    parser.add_argument('-e', '--epochs', default=1)  # Number of epochs for which to train the model
    parser.add_argument('-r', '--restore', default=0)  # Binary to restore from a model. 0 = No restore.
    parser.add_argument('-m', '--model_restore', default=1)  # Restores from /save_directory/model_directory/Model[n]
    parser.add_argument('-f', '--file_epoch', default=1)  # Restore filename: 'part_[f].ckpt.meta'
    parser.add_argument('-t', '--train', default=1)  # Binary to train model. 0 = No train.
    parser.add_argument('-v', '--eval', default=1)  # Binary to evalulate model. 0 = No eval.
    args = vars(parser.parse_args())

    # Set Arguments
    flags['num_epochs'] = int(args['epochs'])
    flags['restore_num'] = int(args['model_restore'])
    flags['run_num'] = int(args['run_num'])
    if args['restore'] == 0:
        flags['restore'] = False
    else:
        flags['restore'] = True
        flags['restore_file'] = 'part_' + str(args['file_epoch']) + '.ckpt.meta'
    model = FasterRcnnConv5(flags)
    if int(args['train']) == 1:
        model.train()
    if int(args['eval']) == 1:
        model.test_print_image()
        model.test()
    model.close()


if __name__ == "__main__":
    main()
