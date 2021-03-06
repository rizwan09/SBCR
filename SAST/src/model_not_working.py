import tensorflow as tf
from tensorflow.estimator import ModeKeys
import constants
import evaluation_fns
import attention_fns
import value_fns
import output_fns
import transformer
import nn_utils
import train_utils
import tf_utils
import util
import sys, pdb
from lazy_adam_v2 import LazyAdamOptimizer

class LISAModel:

  def __init__(self, hparams, model_config, task_config, attention_config, feature_idx_map, label_idx_map,
               vocab, nbatches=8):
    self.train_hparams = hparams
    self.test_hparams = train_utils.copy_without_dropout(hparams)

    self.model_config = model_config
    self.task_config = task_config
    self.attention_config = attention_config
    self.feature_idx_map = feature_idx_map
    self.label_idx_map = label_idx_map
    self.vocab = vocab
    self._gradients = []
    self._avg_gradients = None
    self.nbatches = nbatches
    self.theshold_batch_size = 16
    


  def hparams(self, mode):
    if mode == ModeKeys.TRAIN:
      return self.train_hparams
    return self.test_hparams

  def get_embedding_table(self, name, embedding_dim, include_oov, pretrained_fname=None, num_embeddings=None):

    with tf.variable_scope("%s_embeddings" % name):
      initializer = tf.random_normal_initializer()
      if pretrained_fname:
        pretrained_embeddings = util.load_pretrained_embeddings(pretrained_fname)
        initializer = tf.constant_initializer(pretrained_embeddings)
        pretrained_num_embeddings, pretrained_embedding_dim = pretrained_embeddings.shape
        if pretrained_embedding_dim != embedding_dim:
          util.fatal_error("Pre-trained %s embedding dim does not match specified dim (%d vs %d)." %
                           (name, pretrained_embedding_dim, embedding_dim))
        if num_embeddings and num_embeddings != pretrained_num_embeddings:
          util.fatal_error("Number of pre-trained %s embeddings does not match specified "
                           "number of embeddings (%d vs %d)." % (name, pretrained_num_embeddings, num_embeddings))
        num_embeddings = pretrained_num_embeddings

      embedding_table = tf.get_variable(name="embeddings", shape=[num_embeddings, embedding_dim],
                                        initializer=initializer)

      if include_oov:
        oov_embedding = tf.get_variable(name="oov_embedding", shape=[1, embedding_dim],
                                        initializer=tf.random_normal_initializer())
        embedding_table = tf.concat([embedding_table, oov_embedding], axis=0,
                                    name="embeddings_table")

      return embedding_table

  # def model_fn(self, features, mode):
  def model_minibatch(self, features, parse_tree_features, mode):
    # todo can estimators handle dropout for us or do we need to do it on our own?
    hparams = self.hparams(mode)

    with tf.variable_scope("LISA_mini_batch", reuse=tf.AUTO_REUSE):
    
      batch_shape = tf.shape(features)
      batch_size = batch_shape[0]
      batch_seq_len = batch_shape[1]

      parse_batch_shape = tf.shape(parse_tree_features)
      parse_batch_size = parse_batch_shape[0]
      parse_batch_seq_len = parse_batch_shape[1]

      layer_config = self.model_config['layers']
      sa_hidden_size = layer_config['head_dim'] * layer_config['num_heads']

      feats = {f: features[:, :, idx] for f, idx in self.feature_idx_map.items()}

      # todo this assumes that word_type is always passed in
      words = feats['word_type']

      # for masking out padding tokens
      tokens_to_keep = tf.where(tf.equal(words, constants.PAD_VALUE), tf.zeros([batch_size, batch_seq_len]),
                                tf.ones([batch_size, batch_seq_len]))

      parse_tree_feats={'parse_tree_type': parse_tree_features}
      parse_tree_tokens_to_keep = tf.where(tf.equal(parse_tree_features, constants.PAD_VALUE), tf.zeros([parse_batch_size, parse_batch_seq_len]),
                                tf.ones([parse_batch_size, parse_batch_seq_len]))

      # Extract named features from monolithic "features" input
      feats = {f: tf.multiply(tf.cast(tokens_to_keep, tf.int32), v) for f, v in feats.items()}
      parse_tree_feats = {f: tf.multiply(tf.cast(parse_tree_tokens_to_keep, tf.int32), v) for f, v in parse_tree_feats.items()}
      
      # Extract named labels from monolithic "features" input, and mask them
      # todo fix masking -- is it even necessary?
      labels = {}
      for l, idx in self.label_idx_map.items():
        these_labels = features[:, :, idx[0]:idx[1]] if idx[1] != -1 else features[:, :, idx[0]:]
        these_labels_masked = tf.multiply(these_labels, tf.cast(tf.expand_dims(tokens_to_keep, -1), tf.int32))
        # check if we need to mask another dimension
        if idx[1] == -1:
          last_dim = tf.shape(these_labels)[2]
          this_mask = tf.where(tf.equal(these_labels_masked, constants.PAD_VALUE),
                               tf.zeros([batch_size, batch_seq_len, last_dim], dtype=tf.int32),
                               tf.ones([batch_size, batch_seq_len, last_dim], dtype=tf.int32))
          these_labels_masked = tf.multiply(these_labels_masked, this_mask)
        else:
          these_labels_masked = tf.squeeze(these_labels_masked, -1)
        labels[l] = these_labels_masked

      # load transition parameters
      transition_stats = util.load_transition_params(self.task_config, self.vocab)

      # Create embeddings tables, loading pre-trained if specified
      embeddings = {}
      for embedding_name, embedding_map in self.model_config['embeddings'].items():
        embedding_dim = embedding_map['embedding_dim']
        if 'pretrained_embeddings' in embedding_map:
          input_pretrained_embeddings = embedding_map['pretrained_embeddings']
          include_oov = True
          embedding_table = self.get_embedding_table(embedding_name, embedding_dim, include_oov,
                                                     pretrained_fname=input_pretrained_embeddings)
        else:
          num_embeddings = embedding_dim
          include_oov = True
          embedding_table = self.get_embedding_table(embedding_name, embedding_dim, include_oov,
                                                     num_embeddings=num_embeddings)
        embeddings[embedding_name] = embedding_table
        tf.logging.log(tf.logging.INFO, "Created embeddings for '%s'." % embedding_name)

      # Set up model inputs
      inputs_list = []
      for input_name in self.model_config['inputs']:
        input_values = feats[input_name]
        input_embedding_lookup = tf.nn.embedding_lookup(embeddings[input_name], input_values)
        inputs_list.append(input_embedding_lookup)
        tf.logging.log(tf.logging.INFO, "Added %s to inputs list." % input_name)
      current_input = tf.concat(inputs_list, axis=2)
      current_input = tf.nn.dropout(current_input, hparams.input_dropout)

      #parse_tree_type_input
      parse_tree_inputs_list = []
      for input_name in self.model_config['parse_tree_inputs']:
        input_values = parse_tree_feats[input_name]
        input_embedding_lookup = tf.nn.embedding_lookup(embeddings[input_name], input_values)
        parse_tree_inputs_list.append(input_embedding_lookup)
        tf.logging.log(tf.logging.INFO, "Added %s to parse_tree_inputs list." % input_name)
      if len(parse_tree_inputs_list)>1: current_parse_tree_input = tf.concat(parse_tree_inputs_list, axis=2)
      else: current_parse_tree_input = parse_tree_inputs_list[0]
      current_parse_tree_input = tf.nn.dropout(current_parse_tree_input, hparams.input_dropout)


      with tf.variable_scope('project_input'):
        current_input = nn_utils.MLP(current_input, sa_hidden_size, n_splits=1)
        current_parse_tree_input = nn_utils.MLP(current_parse_tree_input, sa_hidden_size, n_splits=1) #PARSE_BATCH X SEQ X 200


      predictions = {}
      eval_metric_ops = {}
      export_outputs = {}
      loss = tf.constant(0.)
      items_to_log = {}

      num_layers = max(self.task_config.keys()) + 1
      tf.logging.log(tf.logging.INFO, "Creating transformer model with %d layers" % num_layers)
      with tf.variable_scope('transformer'):
        current_input = transformer.add_timing_signal_1d(current_input)
        for i in range(num_layers):
          with tf.variable_scope('layer%d' % i):

            special_attn = []
            special_values = []
            if i in self.attention_config:

              this_layer_attn_config = self.attention_config[i]

              if 'attention_fns' in this_layer_attn_config:
                for attn_fn, attn_fn_map in this_layer_attn_config['attention_fns'].items():
                  attention_fn_params = attention_fns.get_params(mode, attn_fn_map, predictions, feats, labels)
                  this_special_attn = attention_fns.dispatch(attn_fn_map['name'])(**attention_fn_params)
                  special_attn.append(this_special_attn)

              if 'value_fns' in this_layer_attn_config:
                for value_fn, value_fn_map in this_layer_attn_config['value_fns'].items():
                  value_fn_params = value_fns.get_params(mode, value_fn_map, predictions, feats, labels, embeddings)
                  this_special_values = value_fns.dispatch(value_fn_map['name'])(**value_fn_params)
                  special_values.append(this_special_values)

            current_input = transformer.transformer(current_input, tokens_to_keep, layer_config['head_dim'],
                                                    layer_config['num_heads'], hparams.attn_dropout,
                                                    hparams.ff_dropout, hparams.prepost_dropout,
                                                    layer_config['ff_hidden_size'], special_attn, special_values)
            current_parse_tree_input = transformer.transformer(current_parse_tree_input, parse_tree_tokens_to_keep, layer_config['head_dim'],
                                                    layer_config['num_heads'], hparams.attn_dropout,
                                                    hparams.ff_dropout, hparams.prepost_dropout,
                                                    layer_config['ff_hidden_size'], special_attn, special_values)

            if i in self.task_config:

              # if normalization is done in layer_preprocess, then it should also be done
              # on the output, since the output can grow very large, being the sum of
              # a whole stack of unnormalized layer outputs.
              current_input = transformer.syntax_aware_semantic(current_input, current_parse_tree_input, tokens_to_keep, parse_tree_tokens_to_keep, layer_config['head_dim'],
                                                    layer_config['num_heads'], hparams.attn_dropout,
                                                    hparams.ff_dropout, hparams.prepost_dropout,
                                                    layer_config['ff_hidden_size'], special_attn, special_values)
              current_input = nn_utils.layer_norm(current_input)

              # todo test a list of tasks for each layer
              for task, task_map in self.task_config[i].items():
                task_labels = labels[task]
                task_vocab_size = self.vocab.vocab_names_sizes[task] if task in self.vocab.vocab_names_sizes else -1

                # Set up CRF / Viterbi transition params if specified
                with tf.variable_scope("crf"):  # to share parameters, change scope here
                  # transition_stats_file = task_map['transition_stats'] if 'transition_stats' in task_map else None
                  task_transition_stats = transition_stats[task] if task in transition_stats else None

                  # create transition parameters if training or decoding with crf/viterbi
                  task_crf = 'crf' in task_map and task_map['crf']
                  task_viterbi_decode = task_crf or 'viterbi' in task_map and task_map['viterbi']
                  transition_params = None
                  if task_viterbi_decode or task_crf:
                    transition_params = tf.get_variable("transitions", [task_vocab_size, task_vocab_size],
                                                        initializer=tf.constant_initializer(task_transition_stats),
                                                        trainable=task_crf)
                    train_or_decode_str = "training" if task_crf else "decoding"
                    tf.logging.log(tf.logging.INFO, "Created transition params for %s %s" % (train_or_decode_str, task))

                output_fn_params = output_fns.get_params(mode, self.model_config, task_map['output_fn'], predictions,
                                                         feats, labels, current_input, task_labels, task_vocab_size,
                                                         self.vocab.joint_label_lookup_maps, tokens_to_keep,
                                                         transition_params, hparams)
                task_outputs = output_fns.dispatch(task_map['output_fn']['name'])(**output_fn_params)

                # want task_outputs to have:
                # - predictions
                # - loss
                # - scores
                # - probabilities
                predictions[task] = task_outputs

                # do the evaluation
                for eval_name, eval_map in task_map['eval_fns'].items():
                  eval_fn_params = evaluation_fns.get_params(task_outputs, eval_map, predictions, feats, labels,
                                                             task_labels, self.vocab.reverse_maps, tokens_to_keep)
                  eval_result = evaluation_fns.dispatch(eval_map['name'])(**eval_fn_params)
                  eval_metric_ops[eval_name] = eval_result

                # get the individual task loss and apply penalty
                this_task_loss = task_outputs['loss'] * task_map['penalty']

                # log this task's loss
                items_to_log['%s_loss' % task] = this_task_loss

                # add this loss to the overall loss being minimized
                loss += this_task_loss

    return predictions, loss, eval_metric_ops

  def append_minibatch_results(self, predictions_minibatch, eval_metric_ops_minibatch,
    predictions, eval_metric_ops, first_minbatch=0):
    for k1, v1 in predictions_minibatch.items():
        if first_minbatch==1: predictions[k1] = {}
        for k2, v2 in v1.items():
          if first_minbatch==1: predictions[k1][k2] = v2
          else: 
            if 'loss' in k2: 
              predictions[k1][k2] = (predictions[k1][k2] + v2)/2
            else:
              predictions[k1][k2] = tf.concat([predictions[k1][k2], v2], axis = 0)

    for k1, v1 in eval_metric_ops_minibatch.items():
      if first_minbatch==1: eval_metric_ops[k1] = []
      else: eval_metric_ops[k1] = list(eval_metric_ops[k1])
      for i in range(len(v1)):
        if first_minbatch==1: eval_metric_ops[k1].append(v1[i])
        else: eval_metric_ops[k1][i] = (eval_metric_ops[k1][i]+v1[i])/2 #avg
      eval_metric_ops[k1] = tuple(eval_metric_ops[k1])
    # loss_array.append(loss_minibatch)

  def model_fn(self, features, mode):
    
    # todo can estimators handle dropout for us or do we need to do it on our own?
    hparams = self.hparams(mode)

    with tf.variable_scope("LISA", reuse=tf.AUTO_REUSE):
      features_labels = features
      features = features_labels['features']
      parse_tree_features = features_labels['parse_tree']

      batch_shape = tf.shape(features)
      batch_size = batch_shape[0]
      batch_seq_len = batch_shape[1]

      parse_batch_shape = tf.shape(parse_tree_features)
      parse_batch_size = parse_batch_shape[0]
      parse_batch_seq_len = parse_batch_shape[1]

      layer_config = self.model_config['layers']
      sa_hidden_size = layer_config['head_dim'] * layer_config['num_heads']

      feats = {f: features[:, :, idx] for f, idx in self.feature_idx_map.items()}

      # todo this assumes that word_type is always passed in
      words = feats['word_type']

      # for masking out padding tokens
      tokens_to_keep = tf.where(tf.equal(words, constants.PAD_VALUE), tf.zeros([batch_size, batch_seq_len]),
                                tf.ones([batch_size, batch_seq_len]))

      parse_tree_feats={'parse_tree_type': parse_tree_features}
      parse_tree_tokens_to_keep = tf.where(tf.equal(parse_tree_features, constants.PAD_VALUE), tf.zeros([parse_batch_size, parse_batch_seq_len]),
                                tf.ones([parse_batch_size, parse_batch_seq_len]))

      # Extract named features from monolithic "features" input
      feats = {f: tf.multiply(tf.cast(tokens_to_keep, tf.int32), v) for f, v in feats.items()}
      parse_tree_feats = {f: tf.multiply(tf.cast(parse_tree_tokens_to_keep, tf.int32), v) for f, v in parse_tree_feats.items()}

      # Extract named labels from monolithic "features" input, and mask them
      # todo fix masking -- is it even necessary?
      labels = {}
      for l, idx in self.label_idx_map.items():
        these_labels = features[:, :, idx[0]:idx[1]] if idx[1] != -1 else features[:, :, idx[0]:]
        these_labels_masked = tf.multiply(these_labels, tf.cast(tf.expand_dims(tokens_to_keep, -1), tf.int32))
        # check if we need to mask another dimension
        if idx[1] == -1:
          last_dim = tf.shape(these_labels)[2]
          this_mask = tf.where(tf.equal(these_labels_masked, constants.PAD_VALUE),
                               tf.zeros([batch_size, batch_seq_len, last_dim], dtype=tf.int32),
                               tf.ones([batch_size, batch_seq_len, last_dim], dtype=tf.int32))
          these_labels_masked = tf.multiply(these_labels_masked, this_mask)
        else:
          these_labels_masked = tf.squeeze(these_labels_masked, -1)
        labels[l] = these_labels_masked

      # load transition parameters
      transition_stats = util.load_transition_params(self.task_config, self.vocab)

      # Create embeddings tables, loading pre-trained if specified
      embeddings = {}
      for embedding_name, embedding_map in self.model_config['embeddings'].items():
        embedding_dim = embedding_map['embedding_dim']
        if 'pretrained_embeddings' in embedding_map:
          input_pretrained_embeddings = embedding_map['pretrained_embeddings']
          include_oov = True
          embedding_table = self.get_embedding_table(embedding_name, embedding_dim, include_oov,
                                                     pretrained_fname=input_pretrained_embeddings)
        else:
          num_embeddings = embedding_dim
          include_oov = True
          embedding_table = self.get_embedding_table(embedding_name, embedding_dim, include_oov,
                                                     num_embeddings=num_embeddings)
        embeddings[embedding_name] = embedding_table
        tf.logging.log(tf.logging.INFO, "Created embeddings for '%s'." % embedding_name)

      # Set up model inputs
      inputs_list = []
      for input_name in self.model_config['inputs']:
        input_values = feats[input_name]
        input_embedding_lookup = tf.nn.embedding_lookup(embeddings[input_name], input_values)
        inputs_list.append(input_embedding_lookup)
        tf.logging.log(tf.logging.INFO, "Added %s to inputs list." % input_name)
      current_input = tf.concat(inputs_list, axis=2)
      current_input = tf.nn.dropout(current_input, hparams.input_dropout)

      #parse_tree_type_input
      parse_tree_inputs_list = []
      for input_name in self.model_config['parse_tree_inputs']:
        input_values = parse_tree_feats[input_name]
        input_embedding_lookup = tf.nn.embedding_lookup(embeddings[input_name], input_values)
        parse_tree_inputs_list.append(input_embedding_lookup)
        tf.logging.log(tf.logging.INFO, "Added %s to parse_tree_inputs list." % input_name)
      if len(parse_tree_inputs_list)>1: current_parse_tree_input = tf.concat(parse_tree_inputs_list, axis=2)
      else: current_parse_tree_input = parse_tree_inputs_list[0]
      current_parse_tree_input = tf.nn.dropout(current_parse_tree_input, hparams.input_dropout)


      with tf.variable_scope('project_input'):
        current_input_arrays = []
        current_parse_tree_input_arrays = []

        
        for start_idx in range(0, hparams.get('batch_size'), self.theshold_batch_size):
          end_idx = start_idx+self.theshold_batch_size 
          current_input_minibatch = current_input[start_idx:end_idx]
          current_parse_tree_input_minibatch = current_parse_tree_input[start_idx:end_idx]

          current_input_minibatch = nn_utils.MLP(current_input_minibatch, sa_hidden_size, n_splits=1)
          current_parse_tree_input_minibatch = nn_utils.MLP(current_parse_tree_input_minibatch, sa_hidden_size, n_splits=1) #PARSE_BATCH X SEQ X 200

          current_input_arrays.append(current_input_minibatch)
          current_parse_tree_input_arrays.append(current_parse_tree_input_minibatch)

        current_input = tf.concat(current_input_arrays, axis=0)
        current_parse_tree_input = tf.concat(current_parse_tree_input_arrays, axis=0)






      predictions = {}
      eval_metric_ops = {}
      export_outputs = {}
      loss = tf.constant(0.)
      items_to_log = {}
      loss_array_of_minibatches = [ [] for start_idx in range(0, hparams.get('batch_size'), self.theshold_batch_size)]

      num_layers = max(self.task_config.keys()) + 1
      tf.logging.log(tf.logging.INFO, "Creating transformer model with %d layers" % num_layers)


      with tf.variable_scope('transformer'):

        current_input_arrays = []
        current_parse_tree_input_arrays = []

        
        for start_idx in range(0, hparams.get('batch_size'), self.theshold_batch_size):
          end_idx = start_idx+self.theshold_batch_size 
          current_input_minibatch = current_input[start_idx:end_idx]
          current_parse_tree_input_minibatch = current_parse_tree_input[start_idx:end_idx]

          current_input_minibatch = transformer.add_timing_signal_1d(current_input_minibatch)
          current_parse_tree_input_minibatch = transformer.add_timing_signal_1d(current_parse_tree_input_minibatch)

          current_input_arrays.append(current_input_minibatch)
          current_parse_tree_input_arrays.append(current_parse_tree_input_minibatch)

        current_input = tf.concat(current_input_arrays, axis=0)
        current_parse_tree_input = tf.concat(current_parse_tree_input_arrays, axis=0)




        for i in range(num_layers):
          with tf.variable_scope('layer%d' % i):

            special_attn = []
            special_values = []
            if i in self.attention_config:

              this_layer_attn_config = self.attention_config[i]

              if 'attention_fns' in this_layer_attn_config:
                for attn_fn, attn_fn_map in this_layer_attn_config['attention_fns'].items():
                  attention_fn_params = attention_fns.get_params(mode, attn_fn_map, predictions, feats, labels)
                  this_special_attn = attention_fns.dispatch(attn_fn_map['name'])(**attention_fn_params)
                  special_attn.append(this_special_attn)

              if 'value_fns' in this_layer_attn_config:
                for value_fn, value_fn_map in this_layer_attn_config['value_fns'].items():
                  value_fn_params = value_fns.get_params(mode, value_fn_map, predictions, feats, labels, embeddings)
                  this_special_values = value_fns.dispatch(value_fn_map['name'])(**value_fn_params)
                  special_values.append(this_special_values)
            

            current_input_arrays = []
            current_parse_tree_input_arrays = []

            for start_idx in range(0, hparams.get('batch_size'), self.theshold_batch_size):
              end_idx = start_idx+self.theshold_batch_size 
              current_input_minibatch = current_input[start_idx:end_idx]
              current_parse_tree_input_minibatch = current_parse_tree_input[start_idx:end_idx]

              current_input_minibatch = transformer.transformer(current_input_minibatch, tokens_to_keep[start_idx:end_idx], layer_config['head_dim'],
                                                  layer_config['num_heads'], hparams.attn_dropout,
                                                  hparams.ff_dropout, hparams.prepost_dropout,
                                                  layer_config['ff_hidden_size'], special_attn, special_values)
              current_parse_tree_input_minibatch = transformer.transformer(current_parse_tree_input_minibatch, parse_tree_tokens_to_keep[start_idx:end_idx], layer_config['head_dim'],
                                                    layer_config['num_heads'], hparams.attn_dropout,
                                                    hparams.ff_dropout, hparams.prepost_dropout,
                                                    layer_config['ff_hidden_size'], special_attn, special_values)
              current_input_arrays.append(current_input_minibatch)
              current_parse_tree_input_arrays.append(current_parse_tree_input_minibatch)

            current_input = tf.concat(current_input_arrays, axis=0)
            current_parse_tree_input = tf.concat(current_parse_tree_input_arrays, axis=0)

            if i in self.task_config:

              # if normalization is done in layer_preprocess, then it should also be done
              # on the output, since the output can grow very large, being the sum of
              # a whole stack of unnormalized layer outputs.
              
              current_input_arrays = []
              current_parse_tree_input_arrays = []

              for start_idx in range(0, hparams.get('batch_size'), self.theshold_batch_size):

                end_idx = start_idx+self.theshold_batch_size 

                current_input_minibatch = current_input[start_idx:end_idx]
                current_parse_tree_input_minibatch = current_parse_tree_input[start_idx:end_idx]

                current_input_minibatch = transformer.syntax_aware_semantic(current_input_minibatch, 
                                                    current_parse_tree_input_minibatch, 
                                                    tokens_to_keep[start_idx:end_idx], 
                                                    parse_tree_tokens_to_keep[start_idx:end_idx], 
                                                    layer_config['head_dim'],layer_config['num_heads'], hparams.attn_dropout,
                                                    hparams.ff_dropout, hparams.prepost_dropout,
                                                    layer_config['ff_hidden_size'], special_attn, special_values)
                current_input_minibatch = nn_utils.layer_norm(current_input_minibatch)
                current_input_arrays.append(current_input_minibatch)
              
              current_input = tf.concat(current_input_arrays, axis=0)
            
              # todo test a list of tasks for each layer

              predictions_minibatch = {}
              eval_metric_ops_minibatch={}


              for task, task_map in self.task_config[i].items():
                task_labels = labels[task]
                task_vocab_size = self.vocab.vocab_names_sizes[task] if task in self.vocab.vocab_names_sizes else -1

                # Set up CRF / Viterbi transition params if specified
                with tf.variable_scope("crf"):  # to share parameters, change scope here
                  # transition_stats_file = task_map['transition_stats'] if 'transition_stats' in task_map else None
                  task_transition_stats = transition_stats[task] if task in transition_stats else None

                  # create transition parameters if training or decoding with crf/viterbi
                  task_crf = 'crf' in task_map and task_map['crf']
                  task_viterbi_decode = task_crf or 'viterbi' in task_map and task_map['viterbi']
                  transition_params = None
                  if task_viterbi_decode or task_crf:
                    transition_params = tf.get_variable("transitions", [task_vocab_size, task_vocab_size],
                                                        initializer=tf.constant_initializer(task_transition_stats),
                                                        trainable=task_crf)
                    train_or_decode_str = "training" if task_crf else "decoding"
                    tf.logging.log(tf.logging.INFO, "Created transition params for %s %s" % (train_or_decode_str, task))

                
                task_outputs_predictions_arrays = []
                task_outputs_scores_arrays = []
                task_outputs_probabilities_arrays = []
                task_outputs_loss_arrays = []
                eval_results_array = {eval_name: [] for eval_name, eval_map in task_map['eval_fns'].items()}



                for start_idx in range(0, hparams.get('batch_size'), self.theshold_batch_size):
                    end_idx = start_idx+self.theshold_batch_size 
                    feats_minibatch = {k: v[start_idx:end_idx] for k,v in feats.items()}
                    labels_minibatch = {k: v[start_idx:end_idx] for k,v in labels.items()}
                
                    output_fn_params = output_fns.get_params(mode, self.model_config, task_map['output_fn'], predictions_minibatch,
                                                         feats_minibatch, labels_minibatch, current_input[start_idx:end_idx], task_labels[start_idx:end_idx], task_vocab_size,
                                                         self.vocab.joint_label_lookup_maps, tokens_to_keep[start_idx:end_idx],
                                                         transition_params, hparams)
                    task_outputs = output_fns.dispatch(task_map['output_fn']['name'])(**output_fn_params)


                    # want task_outputs to have:
                    # - predictions
                    # - loss
                    # - scores
                    # - probabilities

                    task_outputs_predictions_arrays.append(task_outputs['predictions'])
                    task_outputs_scores_arrays.append(task_outputs['scores'])
                    task_outputs_probabilities_arrays.append(task_outputs['probabilities'])
                    task_outputs_loss_arrays.append(task_outputs['loss'])

                    predictions_minibatch[task] = task_outputs
                    # do the evaluation
                    for eval_name, eval_map in task_map['eval_fns'].items():
                      eval_fn_params = evaluation_fns.get_params(task_outputs, eval_map, predictions_minibatch, feats_minibatch, labels_minibatch,
                                                                 task_labels[start_idx:end_idx], self.vocab.reverse_maps, tokens_to_keep[start_idx:end_idx])
                      eval_result = evaluation_fns.dispatch(eval_map['name'])(**eval_fn_params)
                      eval_results_array[eval_name].append(eval_result)

                    # get the individual task loss and apply penalty
                    this_task_loss = task_outputs['loss'] * task_map['penalty']

                    # log this task's loss
                    items_to_log['%s_loss' % task] = this_task_loss

                  # add this loss to the overall loss being minimized
                    loss += this_task_loss
                    loss_array_of_minibatches[start_idx//self.theshold_batch_size].append(this_task_loss)
                  
              
                for eval_name, eval_result_array in eval_results_array.items(): #v is an array of tuples
                  eval_metric_ops[eval_name]= (sum(eval_result_array[:][0])/len(eval_result_array), sum(eval_result_array[:][1])/len(eval_result_array)) # eval result is a tuple of f1, f1_update
                   
                predictions[task]={}
                predictions[task]['predictions'] = tf.concat(task_outputs_predictions_arrays,axis=0)
                predictions[task]['scores'] = tf.concat(task_outputs_scores_arrays,axis=0)
                predictions[task]['probabilities'] = tf.concat(task_outputs_probabilities_arrays,axis=0)
                predictions[task]['loss'] = tf.reduce_mean(task_outputs_loss_arrays)

      loss_array= [tf.add_n(loss_row) for loss_row in loss_array_of_minibatches]
      loss = tf.divide(tf.add_n(loss_array), tf.cast(tf.constant(len(loss_array)), dtype=tf.float32))


      # set up moving average variables
      assign_moving_averages_dep = tf.no_op()
      if hparams.moving_average_decay > 0.:
        moving_averager = tf.train.ExponentialMovingAverage(hparams.moving_average_decay, zero_debias=True,
                                                            num_updates=tf.train.get_global_step())
        moving_average_op = moving_averager.apply(train_utils.get_vars_for_moving_average(hparams.average_norms))
        # tf.logging.log(tf.logging.INFO,
        #                "Using moving average for variables: %s" % str([v.name for v in tf.trainable_variables()])

        tf.add_to_collection(tf.GraphKeys.UPDATE_OPS, moving_average_op)

        # use moving averages of variables if evaluating
        assign_moving_averages_dep = tf.cond(tf.equal(mode, ModeKeys.TRAIN),
                                             lambda: tf.no_op(),
                                             lambda: nn_utils.set_vars_to_moving_average(moving_averager))

      with tf.control_dependencies([assign_moving_averages_dep]):

        items_to_log['loss'] = loss

        # get learning rate w/ decay
        this_step_lr = train_utils.learning_rate(hparams, tf.train.get_global_step())
        items_to_log['lr'] = this_step_lr

        # optimizer = tf.contrib.opt.NadamOptimizer(learning_rate=this_step_lr, beta1=hparams.beta1,
        #                                              beta2=hparams.beta2, epsilon=hparams.epsilon)
        optimizer = LazyAdamOptimizer(learning_rate=this_step_lr, beta1=hparams.beta1,
                                      beta2=hparams.beta2, epsilon=hparams.epsilon,
                                      use_nesterov=hparams.use_nesterov)
        

        gradients, variables = zip(*optimizer.compute_gradients(loss_array[0]))
        gradients, _ = tf.clip_by_global_norm(gradients, hparams.gradient_clip_norm)

        avg_gradients = gradients

        for loss_item in loss_array[1:]:
          gradients_minibatch, variables = zip(*optimizer.compute_gradients(loss_item))
          gradients_minibatch, _ = tf.clip_by_global_norm(gradients_minibatch, hparams.gradient_clip_norm)
          for gd_itm, gd_itm_mnbatch in zip(avg_gradients, gradients_minibatch):
              gd_itm = tf.add(gd_itm_mnbatch, gd_itm)
          #avg the gardients
          for gd_itm in avg_gradients: gd_itm = tf.divide(gd_itm, tf.cast(tf.constant(len(loss_array)), dtype=tf.float32))

        train_op = optimizer.apply_gradients(zip(avg_gradients, variables), global_step=tf.train.get_global_step())

        

        # export_outputs = {'predict_output': tf.estimator.export.PredictOutput({'scores': scores, 'preds': preds})}

        logging_hook = tf.train.LoggingTensorHook(items_to_log, every_n_iter=20)

        # need to flatten the dict of predictions to make Estimator happy
        flat_predictions = {"%s_%s" % (k1, k2): v2 for k1, v1 in predictions.items() for k2, v2 in v1.items()}

        export_outputs = {tf.saved_model.signature_constants.DEFAULT_SERVING_SIGNATURE_DEF_KEY:
                          tf.estimator.export.PredictOutput(flat_predictions)}

        tf.logging.log(tf.logging.INFO,
                       "Created model with %d trainable parameters" % tf_utils.get_num_trainable_parameters())

        return tf.estimator.EstimatorSpec(mode, flat_predictions, loss, train_op, eval_metric_ops,
                                          training_hooks=[logging_hook], export_outputs=export_outputs)

      

      '''#process first batch
      predictions_minibatch, loss_minibatch, eval_metric_ops_minibatch = self.model_minibatch(features[:theshold_batch_size], 
        parse_tree_features[:theshold_batch_size], mode)
      self.append_minibatch_results(predictions_minibatch, loss_minibatch, eval_metric_ops_minibatch,
        predictions, loss_array, eval_metric_ops, first_minbatch=1)
      # process others
      for start_idx in range(theshold_batch_size, hparams.get('batch_size'),theshold_batch_size):
        end_idx = start_idx+theshold_batch_size if (start_idx+theshold_batch_size) < hparams.get('batch_size') else hparams.get('batch_size')
        predictions_minibatch, loss_minibatch, eval_metric_ops_minibatch = self.model_minibatch(features[start_idx:end_idx], 
          parse_tree_features[start_idx:end_idx], mode)
        self.append_minibatch_results(predictions_minibatch, loss_minibatch, eval_metric_ops_minibatch,
          predictions, loss_array, eval_metric_ops, first_minbatch=0)

      for k1, v1 in eval_metric_ops_minibatch.items():
        eval_metric_ops[k1] = tuple(eval_metric_ops[k1])

      # loss = tf.add_n(loss_array)/len(loss_array)
      # pdb.set_trace()



      # set up moving average variables
      assign_moving_averages_dep = tf.no_op()
      if hparams.moving_average_decay > 0.:
        moving_averager = tf.train.ExponentialMovingAverage(hparams.moving_average_decay, zero_debias=True,
                                                            num_updates=tf.train.get_global_step())
        moving_average_op = moving_averager.apply(train_utils.get_vars_for_moving_average(hparams.average_norms))
        # tf.logging.log(tf.logging.INFO,
        #                "Using moving average for variables: %s" % str([v.name for v in tf.trainable_variables()])

        tf.add_to_collection(tf.GraphKeys.UPDATE_OPS, moving_average_op)

        # use moving averages of variables if evaluating
        assign_moving_averages_dep = tf.cond(tf.equal(mode, ModeKeys.TRAIN),
                                             lambda: tf.no_op(),
                                             lambda: nn_utils.set_vars_to_moving_average(moving_averager))

      with tf.control_dependencies([assign_moving_averages_dep]):

        items_to_log['loss'] = loss

        # get learning rate w/ decay
        this_step_lr = train_utils.learning_rate(hparams, tf.train.get_global_step())
        items_to_log['lr'] = this_step_lr

        # optimizer = tf.contrib.opt.NadamOptimizer(learning_rate=this_step_lr, beta1=hparams.beta1,
        #                                              beta2=hparams.beta2, epsilon=hparams.epsilon)
        optimizer = LazyAdamOptimizer(learning_rate=this_step_lr, beta1=hparams.beta1,
                                      beta2=hparams.beta2, epsilon=hparams.epsilon,
                                      use_nesterov=hparams.use_nesterov)
        #process first minibatch
        gradients, variables = zip(*optimizer.compute_gradients(loss_array[0]))
        gradients, _ = tf.clip_by_global_norm(gradients, hparams.gradient_clip_norm)

        #process others
        for loss in loss_array[1:]:
          gradients_minibatch, variables_minibatch = zip(*optimizer.compute_gradients(loss))
          gradients_minibatch, _ = tf.clip_by_global_norm(gradients, hparams.gradient_clip_norm)

          for gd_itm, gd_itm_mnbatch in zip(gradients, gradients_minibatch):
          	# pdb.set_trace()
          	gd_itm = tf.add(gd_itm_mnbatch, gd_itm)

        #avg the gardients
        for gd_itm in gradients: gd_itm = tf.divide(gd_itm, tf.cast(tf.constant(len(loss_array)), dtype=tf.float32))

       
        train_op = optimizer.apply_gradients(zip(gradients, variables), global_step=tf.train.get_global_step())
       
        # export_outputs = {'predict_output': tf.estimator.export.PredictOutput({'scores': scores, 'preds': preds})}

        logging_hook = tf.train.LoggingTensorHook(items_to_log, every_n_iter=200)

        # need to flatten the dict of predictions to make Estimator happy
        flat_predictions = {"%s_%s" % (k1, k2): v2 for k1, v1 in predictions.items() for k2, v2 in v1.items()}

        export_outputs = {tf.saved_model.signature_constants.DEFAULT_SERVING_SIGNATURE_DEF_KEY:
                          tf.estimator.export.PredictOutput(flat_predictions)}

        tf.logging.log(tf.logging.INFO,
                       "Created model with %d trainable parameters" % tf_utils.get_num_trainable_parameters())

        return tf.estimator.EstimatorSpec(mode, flat_predictions, loss, train_op, eval_metric_ops,
                                          training_hooks=[logging_hook], export_outputs=export_outputs)
                                          '''
