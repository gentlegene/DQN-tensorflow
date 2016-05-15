import time
import random
import numpy as np
from tqdm import tqdm
import tensorflow as tf

from utils import inject_summary, get_time
from .base import BaseModel
from .history import History
from .ops import linear, conv2d
from .memory import Memory
from .replay_memory import ReplayMemory

class Agent(BaseModel):
  def __init__(self, config, environment, sess):
    super(Agent, self).__init__(config)

    self.sess = sess
    self.env = environment
    self.history = History(self.config)

    #self.memory = Memory(self.config)
    self.memory = ReplayMemory(self.config)

    self.step_op = tf.Variable(0, trainable=False)
    self.ep_op = tf.Variable(self.ep_start, trainable=False)

    self.build_dqn()

  def train(self):
    tf.initialize_all_variables().run()

    self.update_target_q_network()
    self.load_model()

    start_step = self.step_op.eval()
    start_time = time.time()

    num_game = 0
    total_reward = 0.
    self.total_loss = 0.
    self.total_q = 0.
    self.update_count = 0
    ep_reward = 0.
    max_ep_reward = 0.
    min_ep_reward = 99999.

    screen, reward, action, terminal = self.env.new_random_game()

    for self.step in tqdm(range(start_step, self.max_step), ncols=100, initial=start_step):
      action = self.perceive(screen, reward, action, terminal)

      if terminal:
        screen, reward, action, terminal = self.env.new_random_game()

        min_ep_reward = min(ep_reward, min_ep_reward)
        max_ep_reward = max(ep_reward, max_ep_reward)
        num_game += 1

        ep_reward = 0.
      else:
        screen, reward, terminal = self.env.act(action, is_training=True)
        ep_reward += reward

      total_reward += reward

      if self.step > self.learn_start:
        if self.step % self.test_step == self.test_step - 1:
          avg_reward = total_reward / self.test_step
          avg_loss = self.total_loss / self.update_count
          avg_q = self.total_q / self.update_count

          print "\navg_r: %.4f, avg_l: %.6f, avg_q: %3.6f, max_ep_r: %.4f, min_ep_r: %.4f, # game: %d" \
              % (avg_reward, avg_loss, avg_q, max_ep_reward, min_ep_reward, num_game)

          inject_summary(self.writer, "average/reward", avg_reward, self.step)
          inject_summary(self.writer, "average/loss", avg_loss, self.step)
          inject_summary(self.writer, "average/q", avg_q, self.step)
          inject_summary(self.writer, "episode/max reward", max_ep_reward, self.step)
          inject_summary(self.writer, "episode/min reward", min_ep_reward, self.step)
          inject_summary(self.writer, "episode/# of game", num_game, self.step)

          num_game = 0
          total_reward = 0.
          self.total_loss = 0.
          self.total_q = 0.
          self.update_count = 0
          ep_reward = 0.
          max_ep_reward = 0.
          min_ep_reward = 99999.

        if self.step % self.save_step == self.save_step - 1:
          self.step_op.assign(self.step + 1).eval()
          self.save_model(self.step + 1)

  def play(self, n_step=1000, n_episode=20, test_ep=0.01, render=False):
    test_history = History(self.config)

    self.env.monitor.start('/tmp/%s-%s' % (self.env_name, get_time()))
    for i_episode in xrange(n_episode):
      screen = self.env.new_game()

      for _ in xrange(self.history_length):
        test_history.add(screen)

      for t in xrange(n_step):
        if render: self.env.render()

        if random.random() < test_ep:
          action = random.randint(0, self.env.action_size - 1)
        else:
          action = self.q_action.eval({self.s_t: [self.history.get()]})

        screen, reward, done, _ = self.env.act(action, is_training=False)
        test_history.add(screen)

        if done:
          print "Episode finished after {} timesteps".format(t+1)
          break

    self.env.monitor.close()

  def perceive(self, screen, reward, action, terminal, test_ep=None):
    # reward clipping
    reward = max(self.min_reward, min(self.max_reward, reward))

    # add memory
    # s_t = self.history.get().copy()
    # self.history.add(screen)
    # s_t_plus_1 = self.history.get().copy()

    if test_ep == None:
      self.memory.add(screen, reward, action, terminal)

    # e greedy
    ep = test_ep or (self.ep_end +
        max(0., (self.ep_start - self.ep_end)
          * (self.ep_end_t - max(0., self.step - self.learn_start)) / self.ep_end_t))

    if random.random() < ep:
      action = random.randint(0, self.env.action_size - 1)
    else:
      action = self.q_action.eval({self.s_t: [self.history.get()]})

    if self.step > self.learn_start:
      if test_ep == None and self.step % self.train_frequency == 0:
        self.q_learning_mini_batch()

      if self.step % self.target_q_update_step == self.target_q_update_step - 1:
        self.update_target_q_network()

    return action

  def q_learning_mini_batch(self):
    if self.memory.count < self.history_length:
      return
    else:
      s_t, action, reward, s_t_plus_1, terminal = self.memory.sample()

    t = time.time()
    q_t_plus_1 = self.target_q.eval({self.target_s_t: s_t_plus_1})

    terminal = np.array(terminal) + 0.
    max_q_t_plus_1 = np.max(q_t_plus_1, axis=1)
    target_q_t = (1. - terminal) * self.discount * max_q_t_plus_1 + reward

    _, loss = self.sess.run([self.optim, self.loss], {
      self.target_q_t: target_q_t,
      self.action: action,
      self.s_t: s_t,
    })

    self.total_loss += loss
    self.total_q += q_t_plus_1.mean()
    self.update_count += 1

  def build_dqn(self):
    self.w = {}
    self.t_w = {}

    #initializer = tf.contrib.layers.xavier_initializer()
    initializer = tf.truncated_normal_initializer(0, 0.02)
    activation_fn = tf.nn.relu

    # training network
    if self.cnn_format == 'NCHW':
      self.s_t = tf.placeholder('float32',
          [None, self.history_length, self.screen_width, self.screen_height], name='s_t')
    else:
      self.s_t = tf.placeholder('float32',
          [None, self.screen_width, self.screen_height, self.history_length], name='s_t')

    self.l1, self.w['l1_w'], self.w['l1_b'] = conv2d(self.s_t,
        32, [8, 8], [4, 4], initializer, activation_fn, self.cnn_format, name='l1')
    self.l2, self.w['l2_w'], self.w['l2_b'] = conv2d(self.l1,
        64, [4, 4], [2, 2], initializer, activation_fn, self.cnn_format, name='l2')
    self.l3, self.w['l3_w'], self.w['l3_b'] = conv2d(self.l2,
        64, [3, 3], [1, 1], initializer, activation_fn, self.cnn_format, name='l3')

    shape = self.l3.get_shape().as_list()
    self.l3_flat = tf.reshape(self.l3, [-1, reduce(lambda x, y: x * y, shape[1:])])

    self.l4, self.w['l4_w'], self.w['l4_b'] = linear(self.l3_flat, 512, activation_fn=activation_fn, name='l4')
    self.q, self.w['q_w'], self.w['q_b'] = linear(self.l4, self.env.action_size, name='q')
    self.q_action = tf.argmax(self.q, dimension=1)

    # target network
    if self.cnn_format == 'NCHW':
      self.target_s_t = tf.placeholder('float32', 
          [None, self.history_length, self.screen_width, self.screen_height], name='target_s_t')
    else:
      self.target_s_t = tf.placeholder('float32', 
          [None, self.screen_width, self.screen_height, self.history_length], name='target_s_t')

    self.target_l1, self.t_w['l1_w'], self.t_w['l1_b'] = conv2d(self.target_s_t, 
        32, [8, 8], [4, 4], initializer, activation_fn, self.cnn_format, name='target_l1')
    self.target_l2, self.t_w['l2_w'], self.t_w['l2_b'] = conv2d(self.target_l1,
        64, [4, 4], [2, 2], initializer, activation_fn, self.cnn_format, name='target_l2')
    self.target_l3, self.t_w['l3_w'], self.t_w['l3_b'] = conv2d(self.target_l2,
        64, [3, 3], [1, 1], initializer, activation_fn, self.cnn_format, name='target_l3')

    shape = self.target_l3.get_shape().as_list()
    self.target_l3_flat = tf.reshape(self.target_l3, [-1, reduce(lambda x, y: x * y, shape[1:])])

    self.target_l4, self.t_w['l4_w'], self.t_w['l4_b'] = \
        linear(self.target_l3_flat, 512, activation_fn=activation_fn, name='target_l4')
    self.target_q, self.t_w['q_w'], self.t_w['q_b'] = \
        linear(self.target_l4, self.env.action_size, name='target_q')

    # optimizer
    self.target_q_t = tf.placeholder('float32', [None])
    self.action = tf.placeholder('int64', [None])

    action_one_hot = tf.one_hot(self.action, self.env.action_size, 1.0, 0.0)
    q_acted = tf.reduce_sum(self.q * action_one_hot, reduction_indices=1)

    self.delta = self.target_q_t - q_acted
    self.clipped_delta = tf.clip_by_value(self.delta, self.min_delta, self.max_delta)

    self.loss = tf.reduce_mean(tf.square(self.clipped_delta))
    self.optim = tf.train.RMSPropOptimizer(self.learning_rate, momentum=0.95, epsilon=0.01).minimize(self.loss)

    self.summary = tf.merge_all_summaries()
    self.writer = tf.train.SummaryWriter("./logs/%s" % self.model_dir, self.sess.graph)

  def update_target_q_network(self):
    for name in self.w.keys():
      self.t_w[name].assign(self.w[name].eval()).eval()