#!/usr/bin/env bash


data_dir=$DATA_DIR/conll05st-release-new
train_file=$data_dir/train-set.gz.parse.sdeps.combined.bio
dev_file=$data_dir/dev-set.gz.parse.sdeps.combined.bio
transition_stats=$data_dir/transition_probs.tsv
bucket_boundaries=bucket_boundaries.txt

params=${@:1}

python3 src/train.py \
--train_file $train_file \
--dev_file $dev_file \
--transition_stats $transition_stats \
--bucket_boundaries $bucket_boundaries \
--data_config config/conll05.json \
--model_config config/sa/sa_model.json \
$params

