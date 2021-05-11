# Copyright 2021 Google LLC.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import collections
import enum
import functools
import os
import shutil
import subprocess
from typing import List, Tuple, Any, Optional

from absl import flags
from absl import logging
from absl.testing import parameterized
import numpy as np
import pandas as pd
import tensorflow as tf

from google.protobuf import text_format

from tensorflow_decision_forests import keras
from tensorflow_decision_forests.component.model_plotter import model_plotter
from tensorflow_decision_forests.keras import core
from tensorflow_decision_forests.tensorflow import core as tf_core
from yggdrasil_decision_forests.dataset import synthetic_dataset_pb2
from yggdrasil_decision_forests.learner.decision_tree import decision_tree_pb2
from yggdrasil_decision_forests.learner.random_forest import random_forest_pb2

layers = tf.keras.layers
models = tf.keras.models
optimizers = tf.keras.optimizers
callbacks = tf.keras.callbacks
Normalization = layers.experimental.preprocessing.Normalization
CategoryEncoding = layers.experimental.preprocessing.CategoryEncoding
StringLookup = layers.experimental.preprocessing.StringLookup

Dataset = collections.namedtuple(
    "Dataset", ["train", "test", "semantics", "label", "num_classes"])

# Tf's tf.feature_column_FeatureColumn is not accessible.
FeatureColumn = Any


def data_root_path() -> str:
  return ""


def test_data_path() -> str:
  return os.path.join(data_root_path(),
                      "external/ydf/yggdrasil_decision_forests/test_data")


def tmp_path() -> str:
  return flags.FLAGS.test_tmpdir


def prepare_dataset(train, test, label, num_classes) -> Dataset:
  """Prepares a dataset object."""

  semantics = tf_core.infer_semantic_from_dataframe(train)
  del semantics[label]

  def clean(dataset):
    for key, semantic in semantics.items():
      if semantic == tf_core.Semantic.CATEGORICAL:
        dataset[key] = dataset[key].fillna("")
    return dataset

  train = clean(train)
  test = clean(test)

  return Dataset(
      train=train,
      test=test,
      semantics=semantics,
      label=label,
      num_classes=num_classes)


def train_test_split(dataset: pd.DataFrame,
                     ratio_second: float) -> Tuple[pd.DataFrame, pd.DataFrame]:
  """Splits randomly a dataframe in two."""
  assert ratio_second >= 0.0
  assert ratio_second <= 1.0
  index_second = np.random.rand(len(dataset)) < ratio_second
  return dataset[~index_second], dataset[index_second]


def adult_dataset() -> Dataset:
  """Adult/census binary classification dataset."""

  # Path to dataset.
  dataset_directory = os.path.join(test_data_path(), "dataset")
  train_path = os.path.join(dataset_directory, "adult_train.csv")
  test_path = os.path.join(dataset_directory, "adult_test.csv")

  train = pd.read_csv(train_path)
  test = pd.read_csv(test_path)
  label = "income"

  def clean(ds):
    ds[label] = np.where(ds[label] == ">50K", 1, 0)
    return ds

  train = clean(train)
  test = clean(test)
  return prepare_dataset(train, test, label, num_classes=2)


def iris_dataset() -> Dataset:
  """Iris multi-class classification dataset."""

  # Path to dataset.
  dataset_directory = os.path.join(test_data_path(), "dataset")
  dataset_path = os.path.join(dataset_directory, "iris.csv")
  dataset = pd.read_csv(dataset_path)
  train, test = train_test_split(dataset, ratio_second=0.30)
  label = "class"
  classes = ["setosa", "versicolor", "virginica"]

  def clean(ds):
    ds[label] = ds[label].map(classes.index)
    return ds

  train = clean(train)
  test = clean(test)
  return prepare_dataset(train, test, label, num_classes=len(classes))


def abalone_dataset() -> Dataset:
  """Abalone regression dataset."""

  # Path to dataset.
  dataset_directory = os.path.join(test_data_path(), "dataset")
  dataset_path = os.path.join(dataset_directory, "abalone.csv")
  dataset = pd.read_csv(dataset_path)
  train, test = train_test_split(dataset, ratio_second=0.30)

  return prepare_dataset(train, test, label="Rings", num_classes=1)


def shopping_dataset() -> Dataset:
  """Shopping ranking dataset."""

  # Path to dataset.
  dataset_directory = os.path.join(internal_test_data_path(), "dataset")
  dataset_path = os.path.join(dataset_directory,
                              "shopping_relevance_small1.csv")
  dataset = pd.read_csv(dataset_path)
  train, test = train_test_split(dataset, ratio_second=0.30)

  return prepare_dataset(train, test, label="relevance", num_classes=1)


def z_normalize(value, mean, std):
  return (value - mean) / std


def build_feature_usages(dataset: Dataset,
                         include_semantic: bool) -> List[keras.FeatureUsage]:
  if include_semantic:
    return [
        keras.FeatureUsage(key, semantic=semantic)
        for key, semantic in dataset.semantics.items()
    ]
  else:
    return [
        keras.FeatureUsage(key) for key, semantic in dataset.semantics.items()
    ]


def build_feature_columns(dataset: Dataset, dense: bool) -> List[FeatureColumn]:
  # Build tensorflow feature columns.
  feature_columns = []

  for key, semantic in dataset.semantics.items():
    if semantic == keras.FeatureSemantic.NUMERICAL:
      mean = dataset.train[key].mean()
      std = dataset.train[key].std()
      if std == 0:
        std = 1

      feature_columns.append(
          tf.feature_column.numeric_column(
              key,
              normalizer_fn=functools.partial(z_normalize, mean=mean, std=std)))

    elif semantic == keras.FeatureSemantic.CATEGORICAL:
      vocabulary = dataset.train[key].unique()
      sparse_column = tf.feature_column.categorical_column_with_vocabulary_list(
          key, vocabulary)

      if dense:
        indicator_column = tf.feature_column.indicator_column(sparse_column)
        feature_columns.append(indicator_column)
      else:
        feature_columns.append(sparse_column)

    else:
      assert False

  return feature_columns


def build_preprocessing(dataset: Dataset) -> Tuple[List[Any], List[Any]]:

  raw_inputs = []
  processed_inputs = []

  for key, semantic in dataset.semantics.items():
    raw_input_values = dataset.train[key].values

    if semantic == keras.FeatureSemantic.NUMERICAL:

      normalizer = Normalization()
      normalizer.adapt(raw_input_values)

      raw_input = layers.Input(shape=(1,), name=key)
      processed_input = normalizer(raw_input)

      raw_inputs.append(raw_input)
      processed_inputs.append(processed_input)

    elif semantic == keras.FeatureSemantic.CATEGORICAL:

      if raw_input_values.dtype in [np.int64]:
        # Integer
        raw_input = layers.Input(shape=(1,), name=key, dtype="int64")
        raw_input = layers.minimum([raw_input, 5])
        onehot = CategoryEncoding(
            num_tokens=np.minimum(raw_input_values, 5), output_mode="binary")
        processed_input = onehot(raw_input)

      else:
        # String
        raw_input = layers.Input(shape=(1,), name=key, dtype="string")

        lookup = StringLookup(max_tokens=5, output_mode="binary")
        lookup.adapt(raw_input_values)
        processed_input = lookup(raw_input)

      raw_inputs.append(raw_input)
      processed_inputs.append(processed_input)

    else:
      assert False

  return raw_inputs, processed_inputs


def dataset_to_tf_dataset(
    dataset: Dataset) -> Tuple[tf.data.Dataset, tf.data.Dataset]:
  """Converts a Dataset into a training and testing tf.Datasets."""

  def df_to_ds(df):
    return tf.data.Dataset.from_tensor_slices(
        (dict(df.drop(dataset.label, 1)), df[dataset.label].values))

  train_ds = df_to_ds(dataset.train).shuffle(1024).batch(1024)
  test_ds = df_to_ds(dataset.test).batch(1024)
  return train_ds, test_ds


# The different ways to train a model.
class Signature(enum.Enum):
  # Automatic input discovery.
  AUTOMATIC_FEATURE_DISCOVERY = 1

  # A set of input features is specified with the "features" argument.
  # Feature semantics are not provided.
  FEATURES_WITHOUT_SEMANTIC = 2

  # A set of input features is specified with the "features" argument.
  # Feature semantics are provided.
  FEATURES_WITH_SEMANTIC = 3

  # A preprocessing is given. The output of the preprocessing is a dense tensor.
  DENSE_PREPROCESSING = 4

  # A preprocessing is given. The output of the preprocessing is a dictionary of
  # tensors.
  STRUCTURED_DICTIONARY_PREPROCESSING = 5

  # A preprocessing is given. The output of the preprocessing is a list of
  # tensors.
  STRUCTURED_LIST_PREPROCESSING = 6

  # Similar to "STRUCTURED_PREPROCESSING". But with additional semantic
  # provided.
  STRUCTURED_PREPROCESSING_WITH_SEMANTIC = 7

  # TensorFlow Feature columns with dense output.
  # Deprecated in Keras (Oct. 2020).
  DENSE_FEATURE_COLUMN = 8

  # TensorFlow Feature columns with both dense and sparse, float and int and
  # string outputs.
  ANY_FEATURE_COLUMN = 9


def build_model(signature: Signature, dataset: Dataset, **args) -> models.Model:
  """Builds a model with the different supported signatures.

  Setting nn_baseline=True creates a NN keras model instead. This is useful to
  ensure the unit tests are valid

  Args:
    signature: How to build the model object.
    dataset: Dataset for the training and evaluation.
    **args: Extra arguments for the model.

  Returns:
    A keras model.
  """

  if signature == Signature.AUTOMATIC_FEATURE_DISCOVERY:
    model = keras.RandomForestModel(**args)

  elif signature == Signature.FEATURES_WITHOUT_SEMANTIC:
    features = build_feature_usages(dataset, include_semantic=False)
    model = keras.RandomForestModel(features=features, **args)

  elif signature == Signature.FEATURES_WITH_SEMANTIC:
    features = build_feature_usages(dataset, include_semantic=True)
    model = keras.RandomForestModel(features=features, **args)

  elif signature == Signature.DENSE_PREPROCESSING:
    raw_inputs, processed_inputs = build_preprocessing(dataset)
    processed_inputs = layers.Concatenate()(processed_inputs)
    preprocessing = models.Model(inputs=raw_inputs, outputs=processed_inputs)
    model = keras.RandomForestModel(preprocessing=preprocessing, **args)

  elif signature == Signature.STRUCTURED_DICTIONARY_PREPROCESSING:
    raw_inputs, processed_inputs = build_preprocessing(dataset)
    processed_inputs = {value.name: value for value in processed_inputs}
    preprocessing = models.Model(inputs=raw_inputs, outputs=processed_inputs)
    model = keras.RandomForestModel(preprocessing=preprocessing, **args)

  elif signature == Signature.STRUCTURED_LIST_PREPROCESSING:
    raw_inputs, processed_inputs = build_preprocessing(dataset)
    preprocessing = models.Model(inputs=raw_inputs, outputs=processed_inputs)
    model = keras.RandomForestModel(preprocessing=preprocessing, **args)

  elif signature == Signature.STRUCTURED_PREPROCESSING_WITH_SEMANTIC:
    raw_inputs, processed_inputs = build_preprocessing(dataset)
    processed_inputs = {value.name: value for value in processed_inputs}
    preprocessing = models.Model(inputs=raw_inputs, outputs=processed_inputs)
    features = []
    for key in processed_inputs.keys():
      features.append(keras.FeatureUsage(key))
    model = keras.RandomForestModel(
        preprocessing=preprocessing, features=features, **args)

  elif signature == Signature.DENSE_FEATURE_COLUMN:
    feature_columns = build_feature_columns(dataset, dense=True)
    preprocessing = layers.DenseFeatures(feature_columns)
    model = keras.RandomForestModel(preprocessing=preprocessing, **args)

  elif signature == Signature.ANY_FEATURE_COLUMN:
    feature_columns = build_feature_columns(dataset, dense=False)
    preprocessing = layers.DenseFeatures(feature_columns)
    model = keras.RandomForestModel(preprocessing=preprocessing, **args)

  else:
    assert False

  return model


class TFDFInKerasTest(parameterized.TestCase, tf.test.TestCase):

  def _check_adult_model(self,
                         model,
                         dataset,
                         minimum_accuracy,
                         check_serialization=True):
    """Runs a battery of test on a model compatible with the adult dataset.

    The following tests are run:
      - Run and evaluate the model (before training).
      - Train the model.
      - Run and evaluate the model.
      - Serialize the model to a SavedModel.
      - Run the model is a separate binary (without dependencies to the training
        custom OPs).
      - Move the serialized model to another random location.
      - Load the serialized model.
      - Evaluate and run the loaded model.

    Args:
      model: A non-trained model on the adult dataset.
      dataset: A dataset compatible with the model.
      minimum_accuracy: minimum accuracy.
      check_serialization: If true, check the serialization of the model.
    """
    tf_train, tf_test = dataset_to_tf_dataset(dataset)

    model.compile(metrics=["accuracy"])

    # Evaluate the model before training.
    evaluation = model.evaluate(tf_test)
    logging.info("Pre-training evaluation: %s", evaluation)

    predictions = model.predict(tf_test)
    logging.info("Pre-training predictions: %s", predictions)

    # Train the model.
    model.fit(x=tf_train)
    logging.info("Trained model:")
    model.summary()

    # Plot the model
    plot = model_plotter.plot_model(model)
    plot_path = os.path.join(self.get_temp_dir(), "plot.html")
    logging.info("Plot to %s", plot_path)
    with open(plot_path, "w") as f:
      f.write(plot)

    # Evaluate the trained model.
    evaluation = model.evaluate(tf_test)
    logging.info("Evaluation: %s", evaluation)
    self.assertGreaterEqual(evaluation[1], minimum_accuracy)

    predictions = model.predict(tf_test)
    logging.info("Predictions: %s", predictions)

    if check_serialization:
      tf.keras.backend.clear_session()

      # Export the trained model.
      saved_model_path = os.path.join(self.get_temp_dir(), "saved_model")
      new_saved_model_path = os.path.join(self.get_temp_dir(),
                                          "saved_model_copy")
      logging.info("Saving model to %s", saved_model_path)
      model.save(saved_model_path)

      tf.keras.backend.clear_session()

      logging.info("Run model in separate binary")
      process = subprocess.Popen([
          os.path.join(
              data_root_path(),
              "tensorflow_decision_forests/keras/test_runner"),
          "--model_path", saved_model_path, "--dataset_path",
          os.path.join(test_data_path(), "dataset", "adult_test.csv")
      ],
                                 stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE)
      stdout, stderr = process.communicate()
      logging.info("stdout:\n%s", stdout.decode("utf-8"))
      logging.info("stderr:\n%s", stderr.decode("utf-8"))

      logging.info("Copying model from %s to %s", saved_model_path,
                   new_saved_model_path)

      shutil.copytree(saved_model_path, new_saved_model_path)
      shutil.rmtree(saved_model_path)

      # Load and evaluate the exported trained model.
      logging.info("Loading model from %s", new_saved_model_path)
      loaded_model = models.load_model(new_saved_model_path)
      loaded_model.summary()

      evaluation = loaded_model.evaluate(tf_test)
      logging.info("Loaded model evaluation: %s", evaluation)
      self.assertGreaterEqual(evaluation[1], minimum_accuracy)

      predictions = loaded_model.predict(tf_test)
      logging.info("Loaded model predictions: %s", predictions)

  def _check_adult_model_with_cart(self,
                                   model,
                                   dataset,
                                   check_serialization=True):
    """Instance of _check_model for the adult dataset."""

    self._check_adult_model(
        model=model,
        dataset=dataset,
        minimum_accuracy=0.865,
        check_serialization=check_serialization)

  def _check_adult_model_with_one_hot(self,
                                      model,
                                      dataset,
                                      check_serialization=True):
    """Instance of _check_model for the adult dataset with bad preprocessing."""

    self._check_adult_model(
        model=model,
        dataset=dataset,
        minimum_accuracy=0.859,
        check_serialization=check_serialization)

  def test_model_adult_automatic_discovery(self):
    """Test on the Adult dataset.

    Binary classification.
    """

    dataset = adult_dataset()
    model = build_model(
        signature=Signature.AUTOMATIC_FEATURE_DISCOVERY, dataset=dataset)
    self._check_adult_model_with_cart(model, dataset)

    inspector = model.make_inspector()
    self.assertEqual(inspector.num_trees(), 300)
    self.assertEqual(inspector.task, keras.Task.CLASSIFICATION)
    logging.info("Variable importances:\n%s", inspector.variable_importances())

  def test_model_adult_with_hyperparameter_template_v1(self):
    """Test on the Adult dataset.

    Binary classification.
    """

    dataset = adult_dataset()
    model = keras.RandomForestModel(
        hyperparameter_template="benchmark_rank1@v1")

    self._check_adult_model(
        model=model,
        dataset=dataset,
        minimum_accuracy=0.864,
        check_serialization=True)

  def test_model_adult_with_hyperparameter_template_v2(self):
    """Test on the Adult dataset.

    Binary classification.
    """

    dataset = adult_dataset()
    model = keras.RandomForestModel(hyperparameter_template="benchmark_rank1")

    self._check_adult_model(
        model=model,
        dataset=dataset,
        minimum_accuracy=0.864,
        check_serialization=True)

  def test_model_adult_automatic_discovery_oob_variable_importance(self):

    dataset = adult_dataset()
    model = keras.RandomForestModel(compute_oob_variable_importances=True)
    model.fit(keras.pd_dataframe_to_tf_dataset(dataset.train, dataset.label))

    inspector = model.make_inspector()
    logging.info("Variable importances:\n%s", inspector.variable_importances())
    logging.info("OOB Evaluation:\n%s", inspector.evaluation())
    self.assertIn("MEAN_DECREASE_IN_ACCURACY", inspector.variable_importances())
    self.assertGreater(inspector.evaluation().accuracy, 0.86)

  def test_model_adult_automatic_discovery_cart(self):
    """Test on the Adult dataset.

    Binary classification with the Cart learner.
    """

    dataset = adult_dataset()
    model = keras.CartModel()
    self._check_adult_model(
        model=model,
        dataset=dataset,
        minimum_accuracy=0.853,
        check_serialization=True)

  def test_model_adult_automatic_discovery_cart_pandas_dataframe(self):
    """Test the support of pandas dataframes."""

    dataset = adult_dataset()
    model = keras.CartModel()
    model.compile(metrics=["accuracy"])

    # Train the model.
    model.fit(
        keras.pd_dataframe_to_tf_dataset(
            dataset.train, dataset.label, task=keras.Task.CLASSIFICATION))

    # Evaluate the model.
    evaluation = model.evaluate(
        keras.pd_dataframe_to_tf_dataset(
            dataset.test, dataset.label, task=keras.Task.CLASSIFICATION))
    self.assertGreaterEqual(evaluation[1], 0.853)

    # Generate predictions with a dataset containing labels (i.e. the label
    # are ignored).
    prediction_1 = model.predict(
        keras.pd_dataframe_to_tf_dataset(
            dataset.test, dataset.label, task=keras.Task.CLASSIFICATION))
    logging.info("prediction_1 %s", prediction_1)

    # Generate predictions with a dataset without labels.
    prediction_2 = model.predict(keras.pd_dataframe_to_tf_dataset(dataset.test))
    logging.info("prediction_2 %s", prediction_2)

  def test_save_model_without_evaluation(self):
    """Train and save the model without evaluating it.

    The evaluation or prediction functions are automatically building the graph
    used when saving the model. This test ensures the train function also build
    a graph.
    """

    dataset = adult_dataset()
    model = keras.CartModel()
    tf_train, tf_test = dataset_to_tf_dataset(dataset)

    model.fit(tf_train)

    # Export the model.
    saved_model_path = os.path.join(self.get_temp_dir(), "saved_model")
    model.save(saved_model_path)

    # Load and evaluate the exported trained model.
    logging.info("Loading model from %s", saved_model_path)
    loaded_model = models.load_model(saved_model_path)
    loaded_model.summary()

    loaded_model.compile(metrics=["accuracy"])
    evaluation = loaded_model.evaluate(tf_test)
    logging.info("Loaded model evaluation: %s", evaluation)
    self.assertGreaterEqual(evaluation[1], 0.853)

  def test_model_adult_features_without_semantic(self):
    dataset = adult_dataset()
    model = build_model(
        signature=Signature.FEATURES_WITHOUT_SEMANTIC, dataset=dataset)
    self._check_adult_model_with_cart(model, dataset)

  def test_model_adult_features_with_semantic(self):
    dataset = adult_dataset()
    model = build_model(
        signature=Signature.FEATURES_WITH_SEMANTIC, dataset=dataset)
    self._check_adult_model_with_cart(model, dataset)

  def test_model_adult_structured_preprocessing(self):
    dataset = adult_dataset()
    model = build_model(
        signature=Signature.STRUCTURED_LIST_PREPROCESSING, dataset=dataset)
    self._check_adult_model_with_one_hot(model, dataset)

  def test_model_adult_structured_dictionary_preprocessing(self):
    dataset = adult_dataset()
    model = build_model(
        signature=Signature.STRUCTURED_DICTIONARY_PREPROCESSING,
        dataset=dataset,
        num_trees=100)
    self._check_adult_model_with_one_hot(model, dataset)

  def test_model_adult_structured_preprocessing_with_semantic(self):
    dataset = adult_dataset()
    model = build_model(
        signature=Signature.STRUCTURED_PREPROCESSING_WITH_SEMANTIC,
        dataset=dataset,
        num_trees=100)
    self._check_adult_model_with_one_hot(model, dataset)

  def test_model_adult_dense_feature_columns(self):
    dataset = adult_dataset()
    model = build_model(
        signature=Signature.DENSE_FEATURE_COLUMN, dataset=dataset)
    # The z-normalization of numerical feature columns cannot be serialized
    # (25 Nov.2020).
    self._check_adult_model_with_one_hot(
        model, dataset, check_serialization=False)

  def test_model_adult_dense_nparray(self):
    dataset = adult_dataset()
    feature_columns = build_feature_columns(dataset, dense=True)
    dense_features = layers.DenseFeatures(feature_columns)

    train_x = dense_features(dict(dataset.train)).numpy()
    train_y = dataset.train[dataset.label].values
    test_x = dense_features(dict(dataset.test)).numpy()
    test_y = dataset.test[dataset.label].values

    model = build_model(
        signature=Signature.AUTOMATIC_FEATURE_DISCOVERY, dataset=dataset)

    model.compile(metrics=["accuracy"])

    evaluation = model.evaluate(test_x, test_y)
    logging.info("Pre-training evaluation: %s", evaluation)

    predictions = model.predict(test_x)
    logging.info("Pre-training predictions: %s", predictions)

    model.fit(x=train_x, y=train_y, validation_data=(test_x, test_y))
    model.summary()

    evaluation = model.evaluate(test_x, test_y)
    logging.info("Evaluation: %s", evaluation)
    self.assertGreaterEqual(evaluation[1], 0.82)  # Accuracy

    predictions = model.predict(test_x)
    logging.info("Predictions: %s", predictions)

  def test_model_adult_dense_tfdataset(self):

    dataset = adult_dataset()
    feature_columns = build_feature_columns(dataset, dense=True)
    dense_features = layers.DenseFeatures(feature_columns)

    train_x = dense_features(dict(dataset.train))
    train_y = dataset.train[dataset.label].values
    test_x = dense_features(dict(dataset.test))
    test_y = dataset.test[dataset.label].values

    train_ds = tf.data.Dataset.from_tensor_slices((train_x, train_y))
    test_ds = tf.data.Dataset.from_tensor_slices((test_x, test_y))

    train_ds = train_ds.shuffle(1024).batch(64)
    test_ds = test_ds.batch(64)

    model = build_model(
        signature=Signature.AUTOMATIC_FEATURE_DISCOVERY, dataset=dataset)

    model.compile(metrics=["accuracy"])

    model.fit(x=train_ds, validation_data=test_ds)
    model.summary()
    evaluation = model.evaluate(test_ds)
    logging.info("Evaluation: %s", evaluation)
    self.assertGreaterEqual(evaluation[1], 0.82)  # Accuracy

  def test_model_iris(self):
    """Test on the Iris dataset.

    Multi-class classification.
    """

    dataset = iris_dataset()

    logging.info("Dataset:\n%s", dataset.train.head())

    tf_train, tf_test = dataset_to_tf_dataset(dataset)

    model = build_model(
        signature=Signature.AUTOMATIC_FEATURE_DISCOVERY, dataset=dataset)

    model.compile(metrics=["accuracy"])

    model.fit(x=tf_train, validation_data=tf_test)
    model.summary()
    evaluation = model.evaluate(tf_test)
    logging.info("Evaluation: %s", evaluation)
    self.assertGreaterEqual(evaluation[1], 0.90)  # Accuracy

    predictions = model.predict(tf_test)
    logging.info("Predictions: %s", predictions)

  def test_model_abalone(self):
    """Test on the Abalone dataset.

      Regression.
    """

    dataset = abalone_dataset()
    tf_train, tf_test = dataset_to_tf_dataset(dataset)

    model = build_model(
        signature=Signature.AUTOMATIC_FEATURE_DISCOVERY,
        dataset=dataset,
        task=keras.Task.REGRESSION)

    model.compile(metrics=["mse"])  # REMOVE run_eagerly

    model.fit(x=tf_train, validation_data=tf_test)
    model.summary()
    evaluation = model.evaluate(tf_test)
    logging.info("Evaluation: %s", evaluation)
    self.assertLessEqual(evaluation[1], 6.0)  # mse

    predictions = model.predict(tf_test)
    logging.info("Predictions: %s", predictions)

  def test_model_abalone_advanced_config(self):
    """Test on the Abalone dataset."""

    dataset = abalone_dataset()
    tf_train, tf_test = dataset_to_tf_dataset(dataset)

    # Disable the pre-sorting of the numerical features.
    yggdrasil_training_config = keras.core.YggdrasilTrainingConfig()
    rf_training_config = yggdrasil_training_config.Extensions[
        random_forest_pb2.random_forest_config]
    rf_training_config.decision_tree.internal.sorting_strategy = decision_tree_pb2.DecisionTreeTrainingConfig.Internal.SortingStrategy.IN_NODE

    # Train on 10 threads.
    yggdrasil_deployment_config = keras.core.YggdrasilDeploymentConfig(
        num_threads=10)

    model = keras.RandomForestModel(
        task=keras.Task.REGRESSION,
        advanced_arguments=keras.AdvancedArguments(
            yggdrasil_training_config=yggdrasil_training_config,
            yggdrasil_deployment_config=yggdrasil_deployment_config))

    model.compile(metrics=["mse"])  # REMOVE run_eagerly
    model.fit(x=tf_train, validation_data=tf_test)
    model.summary()
    evaluation = model.evaluate(tf_test)
    logging.info("Evaluation: %s", evaluation)
    self.assertLessEqual(evaluation[1], 6.0)  # mse

    predictions = model.predict(tf_test)
    logging.info("Predictions: %s", predictions)

  def _synthetic_train_and_test(
      self,
      task: keras.Task,
      limit_eval_train: float,
      limit_eval_test: float,
      test_numerical: Optional[bool] = False,
      test_multidimensional_numerical: Optional[bool] = False,
      test_categorical: Optional[bool] = False,
      test_categorical_set: Optional[bool] = False):
    """Trains a model on a synthetic dataset."""

    train_path = os.path.join(self.get_temp_dir(), "train.rio.gz")
    test_path = os.path.join(self.get_temp_dir(), "test.rio.gz")
    options = synthetic_dataset_pb2.SyntheticDatasetOptions(
        num_numerical=1 if test_numerical else 0,
        num_categorical=2 if test_categorical else 0,
        num_categorical_set=2 if test_categorical_set else 0,
        num_boolean=1 if test_numerical else 0,
        num_multidimensional_numerical=1
        if test_multidimensional_numerical else 0,
        num_accumulators=3)
    if task == keras.Task.CLASSIFICATION:
      options.classification.num_classes = 2
      options.classification.store_label_as_str = False
    elif task == keras.Task.REGRESSION:
      options.regression.SetInParent()
    elif task == keras.Task.RANKING:
      options.ranking.SetInParent()
    else:
      assert False

    options_path = os.path.join(self.get_temp_dir(), "options.pbtxt")
    with open(options_path, "w") as f:
      f.write(text_format.MessageToString(options))

    logging.info("Create synthetic dataset in %s and %s", train_path, test_path)
    args = [
        "tensorflow_decision_forests/keras/synthetic_dataset",
        "--alsologtostderr", "--train", "tfrecord+tfe:" + train_path, "--test",
        "tfrecord+tfe:" + test_path, "--ratio_test", "0.1", "--options",
        options_path
    ]
    popen = subprocess.Popen(args, stdout=subprocess.PIPE)
    popen.wait()

    feature_spec = {}
    if task == keras.Task.CLASSIFICATION:
      feature_spec["LABEL"] = tf.io.FixedLenFeature([], tf.int64)
    elif task == keras.Task.REGRESSION:
      feature_spec["LABEL"] = tf.io.FixedLenFeature([], tf.float32)
    elif task == keras.Task.RANKING:
      feature_spec["LABEL"] = tf.io.FixedLenFeature([], tf.float32)
      feature_spec["GROUP"] = tf.io.FixedLenFeature([], tf.string)
    else:
      assert False

    if test_numerical:
      feature_spec["num_0"] = tf.io.FixedLenFeature([], tf.float32, np.nan)
      feature_spec["bool_0"] = tf.io.FixedLenFeature([], tf.float32, np.nan)

    if test_multidimensional_numerical:
      feature_spec["multidimensional_num_0"] = tf.io.FixedLenFeature(
          [5], tf.float32, [np.nan] * 5)

    if test_categorical:
      feature_spec["cat_int_0"] = tf.io.FixedLenFeature([], tf.int64, -2)
      feature_spec["cat_str_0"] = tf.io.FixedLenFeature([], tf.string, "")
      feature_spec["cat_int_1"] = tf.io.VarLenFeature(tf.int64)
      feature_spec["cat_str_1"] = tf.io.VarLenFeature(tf.string)

    if test_categorical_set:
      feature_spec["cat_set_int_0"] = tf.io.VarLenFeature(tf.int64)
      feature_spec["cat_set_str_0"] = tf.io.VarLenFeature(tf.string)
      feature_spec["cat_set_int_1"] = tf.io.VarLenFeature(tf.int64)
      feature_spec["cat_set_str_1"] = tf.io.VarLenFeature(tf.string)

    def parse(serialized_example):
      feature_values = tf.io.parse_single_example(
          serialized_example, features=feature_spec)
      label = feature_values.pop("LABEL")
      return feature_values, label

    def preprocess(feature_values, label):
      if test_categorical_set:
        for name in ["cat_set_int_1", "cat_set_str_1"]:
          feature_values[name] = tf.RaggedTensor.from_sparse(
              feature_values[name])

      if task == keras.Task.CLASSIFICATION:
        label = label - 1  # Encode the label in {0,1}.
      return feature_values, label

    train_dataset = tf.data.TFRecordDataset(
        train_path,
        compression_type="GZIP").map(parse).batch(50).map(preprocess)
    test_dataset = tf.data.TFRecordDataset(
        test_path, compression_type="GZIP").map(parse).batch(50).map(preprocess)

    features = []

    if test_categorical_set:
      # The semantic of sparse tensors cannot be inferred safely.
      features.extend([
          keras.FeatureUsage("cat_set_int_0",
                             keras.FeatureSemantic.CATEGORICAL_SET),
          keras.FeatureUsage("cat_set_str_0",
                             keras.FeatureSemantic.CATEGORICAL_SET)
      ])

    if test_categorical:
      # integers are detected as numerical by default.
      features.extend([
          keras.FeatureUsage("cat_int_0", keras.FeatureSemantic.CATEGORICAL),
          keras.FeatureUsage("cat_int_1", keras.FeatureSemantic.CATEGORICAL)
      ])

    if task == keras.Task.CLASSIFICATION:
      model = keras.RandomForestModel(task=task, features=features)
      model.compile(metrics=["accuracy"])
      compare = self.assertGreaterEqual
    elif task == keras.Task.REGRESSION:
      model = keras.RandomForestModel(task=task, features=features)
      model.compile(metrics=["mse"])
      compare = self.assertLessEqual
    elif task == keras.Task.RANKING:
      model = keras.GradientBoostedTreesModel(
          task=task, features=features, ranking_group="GROUP", num_trees=50)
      compare = None
    else:
      assert False

    model.fit(train_dataset)
    model.summary()

    train_evaluation = model.evaluate(train_dataset)
    logging.info("Train evaluation: %s", train_evaluation)
    test_evaluation = model.evaluate(test_dataset)
    logging.info("Test evaluation: %s", test_evaluation)

    # The training evaluation is capped by the ratio of missing value (5%).
    if compare is not None:
      compare(train_evaluation[1], limit_eval_train)
      compare(test_evaluation[1], limit_eval_test)

    _ = model.predict(test_dataset)

  def test_synthetic_classification_numerical(self):
    self._synthetic_train_and_test(
        keras.Task.CLASSIFICATION, 0.8, 0.72, test_numerical=True)

  def test_synthetic_classification_categorical(self):
    self._synthetic_train_and_test(
        keras.Task.CLASSIFICATION, 0.95, 0.70, test_categorical=True)

  def test_synthetic_classification_multidimensional_numerical(self):
    self._synthetic_train_and_test(
        keras.Task.CLASSIFICATION,
        0.96,
        0.70,
        test_multidimensional_numerical=True)

  def test_synthetic_classification_categorical_set(self):
    self._synthetic_train_and_test(
        keras.Task.CLASSIFICATION, 0.915, 0.645, test_categorical_set=True)

  def test_synthetic_regression_numerical(self):
    self._synthetic_train_and_test(
        keras.Task.REGRESSION, 0.41, 0.43, test_numerical=True)

  def test_synthetic_regression_categorical(self):
    self._synthetic_train_and_test(
        keras.Task.REGRESSION, 0.34, 0.34, test_categorical=True)

  def test_synthetic_regression_multidimensional_numerical(self):
    self._synthetic_train_and_test(
        keras.Task.REGRESSION, 0.47, 0.46, test_multidimensional_numerical=True)

  def test_synthetic_regression_categorical_set(self):
    self._synthetic_train_and_test(
        keras.Task.REGRESSION, 0.345, 0.345, test_categorical_set=True)

  def test_synthetic_ranking_numerical(self):
    self._synthetic_train_and_test(
        keras.Task.RANKING, -1.0, -1.0, test_numerical=True)

  def test_model_adult_df_on_top_of_nn(self):
    """Composition of a DF on top of a NN."""

    dataset = adult_dataset()
    tf_train, tf_test = dataset_to_tf_dataset(dataset)

    # Train a NN.
    # Note: The following code does not work with the "models.Sequential" API
    # (Nov.17, 2020).
    raw_inputs, preprocessed_inputs = build_preprocessing(dataset)
    z1 = layers.Concatenate()(preprocessed_inputs)
    z2 = layers.Dense(16, activation=tf.nn.relu6)(z1)
    z3 = layers.Dense(16, activation=tf.nn.relu, name="last")(z2)
    y = layers.Dense(1)(z3)
    nn_model = models.Model(raw_inputs, y)

    nn_model.compile(
        optimizer=optimizers.Adam(),
        loss=tf.keras.losses.BinaryCrossentropy(),
        metrics=["accuracy"])

    nn_model.fit(x=tf_train, validation_data=tf_test, epochs=10)
    logging.info("Trained NN")
    nn_model.summary()

    # Build a DF on top of the NN
    nn_without_head = models.Model(
        inputs=nn_model.inputs, outputs=nn_model.get_layer("last").output)
    df_model = keras.RandomForestModel(preprocessing=nn_without_head)

    df_model.compile(metrics=["accuracy"])

    df_model.fit(x=tf_train, validation_data=tf_test)
    logging.info("Combined model")
    df_model.summary()

  def test_parse_hp_template(self):

    self.assertEqual(core._parse_hp_template("abc@v5"), ("abc", 5))
    self.assertEqual(core._parse_hp_template("abc"), ("abc", None))
    with self.assertRaises(ValueError):
      core._parse_hp_template("abc@5")

  def test_get_matching_template(self):
    a = core.HyperParameterTemplate(
        name="t1", version=1, parameters={"p": 1.0}, description="")
    b = core.HyperParameterTemplate(
        name="t1", version=2, parameters={"p": 2.0}, description="")
    c = core.HyperParameterTemplate(
        name="t2", version=1, parameters={"p": 3.0}, description="")
    templates = [a, b, c]

    self.assertEqual(core._get_matching_template("t1@v1", templates), a)
    self.assertEqual(core._get_matching_template("t1@v2", templates), b)
    self.assertEqual(core._get_matching_template("t1", templates), b)
    self.assertEqual(core._get_matching_template("t2", templates), c)

    with self.assertRaises(ValueError):
      core._get_matching_template("t1@v4", templates)

    with self.assertRaises(ValueError):
      core._get_matching_template("t3", templates)

  def test_apply_hp_template(self):
    templates = [
        core.HyperParameterTemplate(
            name="t1", version=1, parameters={"p1": 2.0}, description="")
    ]

    self.assertEqual(
        core._apply_hp_template({"p1": 1.0},
                                "t1",
                                templates,
                                explicit_parameters=set()), {"p1": 2.0})

    self.assertEqual(
        core._apply_hp_template({"p1": 1.0},
                                "t1",
                                templates,
                                explicit_parameters=set(["p1"])), {"p1": 1.0})

    with self.assertRaises(ValueError):
      core._apply_hp_template({"p1": 1.0},
                              "t2",
                              templates,
                              explicit_parameters=set())

  def test_list_explicit_arguments(self):

    @core._list_explicit_arguments
    def f(a=1, b=2, c=3, explicit_args=None):
      f.last_explicit_args = explicit_args

      del a
      del b
      del c

    f()
    self.assertEqual(f.last_explicit_args, set([]))

    f(a=5)
    self.assertEqual(f.last_explicit_args, set(["a"]))

    f(b=6, c=7)
    self.assertEqual(f.last_explicit_args, set(["b", "c"]))

  def test_rank1_preprocessing(self):
    """Test the limitation on rank1 preprocessing."""

    def experiment(infer_prediction_signature, save_model):
      x_train = [1.0, 2.0, 3.0, 4.0]  # Dataset with a single feature.
      y_train = [0, 1, 0, 1]

      @tf.function(
          input_signature=(tf.TensorSpec(shape=[None], dtype=tf.float32),))
      def processor(x):
        return x + 1

      model = keras.RandomForestModel(
          preprocessing=processor,
          advanced_arguments=keras.AdvancedArguments(
              infer_prediction_signature=infer_prediction_signature))
      model.fit(x=x_train, y=y_train)

      if save_model:
        # Fails if the model is not build before.
        model.save(os.path.join(self.get_temp_dir(), "saved_model"))

    experiment(infer_prediction_signature=False, save_model=False)

    with self.assertRaises(ValueError):
      experiment(infer_prediction_signature=False, save_model=True)

    with self.assertRaises(ValueError):
      experiment(infer_prediction_signature=True, save_model=False)

  def test_get_all_models(self):
    print(keras.get_all_models())


if __name__ == "__main__":
  tf.test.main()