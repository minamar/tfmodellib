# Copyright 2018 Nikolas Hemion. All Rights Reserved.
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

from tfmodellib import TFModel, TFModelConfig, graph_def, docsig, MLP, MLPConfig, build_mlp_graph

import tensorflow as tf


@graph_def
@docsig
def build_vae_graph(input_tensor, latent_size, n_hidden, hidden_activation, output_activation, **kwargs):
    """
    Defines a VAE graph, with `2*len(n_hidden)+1` dense layers:
    
    - `len(n_hidden)` layers for the encoder, where the i-th encoder layer has
      `n_hidden[i]` units.

    - The variational latent (code) layer with `latent_size` units;

    - `len(n_hidden)` layers for the decoder, where the j-th decoder layer has
      `n_hidden[-j-1]` units.
   
    The output layer is of same dimension as the input layer.

    Parameters
    ----------
    input_tensor : Tensor
        The input tensor of size NUM_SAMPLES x INPUT_SIZE.

    latent_size : int
        Number of units in the MLP's output layer.

    n_hidden : list
        List of ints, specifying the number of units for each hidden layer.

    activation : function
        Activation function for the encoder- and decoder-layers.

    For additional *kwargs*, see tfmodellib.build_mlp_graph.

    Returns
    -------
    reconstruction : Tensor
        The output (reconstruction of the input).

    latent_layer : Tensor
        The latent code (sampled from noise distribution).

    latent_mean : Tensor
        The mean of the distribution onto which the input is mapped.

    See Also
    --------
    tfmodellib.build_mlp_graph : Used to construct the encoder and decoder
                                 sub-networks of the autoencoder.
    tfmodellib.build_autoencoder_graph : Standard (non-variational)
                                         autoencoder.
    """

    # define encoder
    with tf.variable_scope('encoder'):
        encoder_out = build_mlp_graph(
                input_tensor=input_tensor,
                out_size=n_hidden[-1],
                n_hidden=n_hidden[:-1],
                hidden_activation=hidden_activation,
                output_activation=hidden_activation,
                **kwargs)

    # define latent mean, sigma_sq, randn
    latent_mean = tf.layers.dense(encoder_out, units=latent_size, activation=None)

    # For the computation of the loss, we need:
    #     sigma**2
    #     log(sigma**2)
    # Other implementation of a VAE map the encoder output onto log(sigma**2).
    # This can however cause numerical problems when computing
    # exp(log(sigma**2)) to obtain sigma**2 (note that
    # float32(exp(88.73))=inf). To avoid this, we instead map the encoder
    # output onto sigma directly, and compute sigma**2 and log(sigma**2) from
    # there (while adding a small constant to sigma**2 before computing the
    # log, in case sigma is exactly 0.0).
    # Furthermore, we use linear activation, which can produce negative values
    # for sigma. We compensate for this by multiplying the random noise with
    # the absolute value of sigma, instead of relying on something like ReLU
    # activation, which could kill off the units.
    latent_sigma = tf.layers.dense(encoder_out, units=latent_size, activation=None)
    latent_sigma_sq = tf.square(latent_sigma)
    latent_log_sigma_sq = tf.log(latent_sigma_sq + 1e-45)
    latent_sigma = tf.abs(latent_sigma)
    latent_randn = tf.random_normal(shape=[latent_size], dtype=tf.float32)

    # define latent layer
    latent_layer = tf.add(latent_mean, tf.multiply(latent_sigma, latent_randn))

    # define decoder
    n_hidden.reverse()
    with tf.variable_scope('decoder'):
        decoder_out = build_mlp_graph(
                input_tensor=latent_layer,
                out_size=n_hidden[-1],
                n_hidden=n_hidden[:-1],
                hidden_activation=hidden_activation,
                output_activation=hidden_activation,
                **kwargs)

    reconstruction = tf.layers.dense(
            inputs=decoder_out, units=input_tensor.shape.as_list()[1],
            activation=output_activation)

    return reconstruction, latent_layer, latent_mean, latent_sigma, latent_sigma_sq, latent_log_sigma_sq


def sum_of_squared_differences(a, b):
    return tf.reduce_sum(tf.squared_difference(a, b), axis=-1)


def mean_of_squared_differences(a, b):
    return tf.reduce_mean(tf.squared_difference(a, b), axis=-1)


class VAEConfig(MLPConfig):

    def init(self):
        self.update(
                in_size=3,
                latent_size=2,
                n_hidden=[10,10],
                hidden_activation=tf.nn.relu,
                output_activation=None,
                optimizer=tf.train.AdamOptimizer,
                use_bn=False,
                # The reconstruction_loss should return a one-dimensional
                # vector of losses (one for each sample), not reduced into a
                # scalar. We compute the mean of these values later, after
                # adding the vector of variational losses (see
                # VAE.build_graph).
                reconstruction_loss=sum_of_squared_differences)
        super(VAEConfig, self).init()

class VAE(MLP):

    def build_graph(self):

        # define input and target placeholder
        self.x_input = tf.placeholder(dtype=tf.float32, shape=[None, self.config['in_size']])
        self.y_target = tf.placeholder(dtype=tf.float32, shape=[None, self.config['in_size']])

        # define the base graph
        kwargs = {}
        if self.config['use_bn']:
            self.bn_is_training = tf.placeholder(dtype=tf.bool, shape=[], name='bn_is_training')
            kwargs = {'bn_is_training': self.bn_is_training}
        kwargs.update(self.config)

        self.y_output, \
        self.latent_layer, \
        self.latent_mean, \
        self.latent_sigma, \
        self.latent_sigma_sq, \
        self.latent_log_sigma_sq = build_vae_graph(input_tensor=self.x_input, **kwargs)

        # define learning rate
        self.learning_rate = tf.placeholder(dtype=tf.float32, shape=[])

        # define loss

        # reconstruction losses for all samples, a vector of shape BATCH_SIZE x 1
        self.reconstruction_losses = self.config['reconstruction_loss'](self.y_target, self.y_output)

        # we keep beta as a placeholder, to allow adjusting it throughout the training.
        self.beta = tf.placeholder(dtype=tf.float32, shape=[])

        # variational (KL) losses for all samples, a vector of shape BATCH_SIZE x 1
        self.variational_losses = 0.5 * self.beta * tf.reduce_sum(
                - 1.0
                - self.latent_log_sigma_sq
                + tf.square(self.latent_mean)
                + self.latent_sigma_sq, axis=-1)

        # combined loss, scalar
        self.loss = tf.reduce_mean(tf.add(self.reconstruction_losses, self.variational_losses), axis=0)

        # define optimizer
        self.optimizer = self.config['optimizer'](learning_rate=self.learning_rate)
        self.minimize_op = self.optimizer.minimize(self.loss)

    def run_update_and_loss(self, batch_inputs, batch_targets, learning_rate, beta):
        ops_to_run = [self.loss, self.minimize_op]
        feed_dict={
                self.x_input: batch_inputs,
                self.y_target: batch_targets,
                self.learning_rate: learning_rate,
                self.beta: beta}
        if self.config['use_bn']:
            ops_to_run += self.sess.graph.get_collection(tf.GraphKeys.UPDATE_OPS)
            feed_dict[self.bn_is_training] = True
        ops_result = self.sess.run(ops_to_run, feed_dict=feed_dict)
        loss = ops_result[0]
        return loss

    def run_loss(self, batch_inputs, batch_targets, learning_rate, beta):
        feed_dict={
                self.x_input: batch_inputs,
                self.y_target: batch_targets,
                self.beta: beta}
        if self.config['use_bn']:
            feed_dict[self.bn_is_training] = False
        loss = self.sess.run(self.loss, feed_dict=feed_dict)
        return loss

if __name__ == '__main__':

    import numpy as np
    import matplotlib.pyplot as plt
    from mpl_toolkits import mplot3d

    # create the model
    conf = VAEConfig(
            in_size=3,
            latent_size=5,
            n_hidden=[150,150],
            hidden_activation=tf.nn.relu,
            output_activation=None,
            reconstruction_loss=mean_of_squared_differences)
    model = VAE(conf)

    # generate some data
    xx,yy = np.random.rand(2*10000).reshape((2,-1))
    z = lambda x, y: (np.sin(10*x) + np.cos(10*y)) * np.exp(-((x-0.5)**2+(y-0.5)**2)/0.1)
    zz = z(xx,yy)
    x = np.vstack((xx.flat,yy.flat,zz.flat)).T

    x -= x.mean(axis=0)
    x /= x.std(axis=0)

    x_train = x[:int(x.shape[0]*0.8)]
    x_valid = x[x_train.shape[0]:]

    # run the training
    for t in range(1000):
        model.train(
                train_inputs=x_train,
                train_targets=x_train,
                validation_inputs=x_valid,
                validation_targets=x_valid,
                batch_size=100,
                learning_rate=0.0001,
                beta=0.01)

    # get estimates
    reconstruction = np.vstack(model.infer(inputs=x_valid, batch_size=None))

    # plot data and estimates
    fig = plt.figure()
    fig_size = fig.get_size_inches()
    fig_size[0] *= 2.5
    fig.set_size_inches(fig_size)

    ax = fig.add_subplot(1,3,1,projection='3d')
    ax.plot(*x_valid.T, ls='none', marker='.', mec='none', mfc='g')
    ax.plot(*reconstruction.T, ls='none', marker='.', mec='none', mfc='b')
    ax.set_title('reconstruction')

    # plot latent mean for the three latent dimensions with the lowest mean
    # standard deviation
    # 
    # if training were to be continued, the VAE might collapse the latent
    # representation into a two-dimensional representation, discovering the x-y
    # space generating the training data.
    l = np.linspace(0.0, 1.0, 10)
    xx,yy = np.meshgrid(l,l)
    zz = z(xx,yy)
    x = np.vstack((xx.flat,yy.flat,zz.flat)).T

    latent_mean, latent_sigma = model.sess.run([model.latent_mean, model.latent_sigma], feed_dict={model.x_input: x})
    latent_sigma_mean = latent_sigma.mean(axis=0)
    dim_inds = np.argsort(latent_sigma_mean)[:3]
    latent_u,latent_v,latent_w = latent_mean[:,dim_inds].reshape((l.size,l.size,3)).transpose((2,0,1))

    ax = fig.add_subplot(1,3,2,projection='3d')
    for ind in range(l.size):
        ax.plot(latent_u[ind], latent_v[ind], latent_w[ind], ls='-', marker='None', color='b')
        ax.plot(latent_u.T[ind], latent_v.T[ind], latent_w.T[ind], ls='-', marker='None', color='b')
    ax.set_title('latent encoding')
    ax.set_xlabel('latent dimension {:d}'.format(dim_inds[0]))
    ax.set_ylabel('latent dimension {:d}'.format(dim_inds[1]))

    # bar plot mean standard deviation of latent dimensions
    ax = fig.add_subplot(1,3,3)
    ax.bar(range(latent_sigma_mean.size), latent_sigma_mean)
    ax.set_title('mean standard deviations')
    ax.set_xlabel('latent dimension')
    ax.set_ylabel('sigma')

    fig.tight_layout()

    plt.show()
