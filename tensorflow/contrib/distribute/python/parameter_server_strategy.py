# Copyright 2018 The TensorFlow Authors. All Rights Reserved.
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
"""Classes implementing a multi-worker ps DistributionStrategy."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from tensorflow.contrib.distribute.python import cross_tower_ops as cross_tower_ops_lib
from tensorflow.contrib.distribute.python import mirrored_strategy
from tensorflow.contrib.distribute.python import values
from tensorflow.python.distribute import multi_worker_util
from tensorflow.python.eager import context
from tensorflow.python.framework import device as tf_device
from tensorflow.python.framework import ops
from tensorflow.python.ops import array_ops
from tensorflow.python.ops import resource_variable_ops
from tensorflow.python.ops import variable_scope as vs
from tensorflow.python.platform import tf_logging as logging
from tensorflow.python.training import device_setter
from tensorflow.python.training import device_util
from tensorflow.python.training import distribute as distribute_lib
from tensorflow.python.util import nest

_LOCAL_CPU = "/device:CPU:0"
_LOCAL_GPU_0 = "/device:GPU:0"


# TODO(yuefengz): maybe cache variables on local CPU.
# TODO(yuefengz): we may want to set session options to disallow communication
# between workers.
class ParameterServerStrategy(distribute_lib.DistributionStrategy):
  """A parameter server DistributionStrategy.

  This strategy class works for both local training and between-graph replicated
  training for multiple workers. If `cluster_spec` is specified, either passed
  in to __init__() method or parsed from the
  ["TF_CONFIG" environment
  variable](https://www.tensorflow.org/api_docs/python/tf/estimator/RunConfig),
  variables and updates to those variables are assigned to parameter servers and
  other operations are assigned to workers. If `cluster_spec` is not set, it
  becomes local training where variables are assigned to local CPU or the only
  GPU. When each worker has more than one GPU, operations will be replicated on
  these GPUs. In both cases, operations are replicated but variables are not and
  these workers share a common view for which paramater server a variable is
  assigned to.

  This class assumes between-graph replication will be used and works on a graph
  for a particular worker. Note that each graph and worker is independent.
  This means that while each worker will synchronously compute a single gradient
  update across all GPUs, updates between workers proceed asynchronously.
  Operations that occur only on the first tower (such as incrementing the global
  step), will occur on the first tower *of every worker*.

  It is expected to call `call_for_each_tower(fn, *args, **kwargs)` for any
  operations which potentially can be replicated across towers (i.e. multiple
  GPUs) even if there is only CPU or one GPU. When defining the `fn`, extra
  caution needs to be taken:

  1) Always use `tf.get_variable` instead of `tf.Variable` which is not able
  to refer to the same variable on different towers.

  2) It is generally not recommended to open a device scope under the strategy's
  scope. A device scope (i.e. calling `tf.device`) will be merged with or
  override the device for operations but will not change the device for
  variables.

  3) It is also not recommended to open a colocation scope (i.e. calling
  `tf.colocate_with`) under the strategy's scope. For colocating variables,
  use `distribution.colocate_vars_with` instead. Colocation of ops will possibly
  create conflicts of device assignment.
  """

  def __init__(self, num_gpus_per_worker=0):
    """Initializes this strategy.

    Args:
      num_gpus_per_worker: number of local GPUs or GPUs per worker, the default
        is 0 meaning CPU only.

    Raises:
      ValueError: if `cluster_spec` is given but `task_type` or `task_id` is
        not.
    """
    super(ParameterServerStrategy, self).__init__()
    self._num_gpus_per_worker = num_gpus_per_worker
    self._initialize_local(num_gpus_per_worker)

    # We typically don't need to do all-reduce in this strategy.
    self._cross_tower_ops = (
        cross_tower_ops_lib.ReductionToOneDeviceCrossTowerOps(
            reduce_to_device=_LOCAL_CPU))

  def _initialize_multi_worker(self, num_gpus_per_worker, cluster_spec,
                               task_type, task_id):
    """Initialize devices for multiple workers.

    It creates variable devices and compute devices. Variables and operations
    will be assigned to them respectively. We have one compute device per tower.
    The variable device is a device function or device string. The default
    variable device assigns variables to parameter servers in a round-robin
    fashion.

    Args:
      num_gpus_per_worker: number of local GPUs or GPUs per worker.
      cluster_spec: a dict, ClusterDef or ClusterSpec object specifying the
        cluster configurations.
      task_type: the current task type.
      task_id: the current task id.

    Raises:
      ValueError: if the cluster_spec doesn't have ps jobs.
    """
    assert cluster_spec
    if not task_type or task_id is None:
      raise ValueError("When `cluster_spec` is given, you must also specify "
                       "`task_type` and `task_id`")
    cluster_spec = multi_worker_util.normalize_cluster_spec(cluster_spec)

    self._worker_device = "/job:%s/task:%d" % (self._task_type, self._task_id)

    # Define compute devices which is a list of device strings and one for each
    # tower. When there are GPUs, replicate operations on these GPUs. Otherwise,
    # place operations on CPU.
    if num_gpus_per_worker > 0:
      self._compute_devices = [
          "%s/device:GPU:%d" % (self._worker_device, i)
          for i in range(num_gpus_per_worker)
      ]
    else:
      self._compute_devices = [self._worker_device]

    self._compute_devices = list(
        map(device_util.resolve, self._compute_devices))
    self._canonical_compute_device_set = set(self._compute_devices)

    # In distributed mode, place variables on ps jobs in a round-robin fashion.
    # Note that devices returned from `replica_device_setter` are not
    # canonical and therefore we don't canonicalize all variable devices to
    # make them consistent.
    # TODO(yuefengz): support passing a strategy object to control variable
    # assignment.
    # TODO(yuefengz): merge the logic of replica_device_setter into this
    # class.
    num_ps_replicas = len(cluster_spec.as_dict().get("ps", []))
    if num_ps_replicas == 0:
      raise ValueError("The cluster spec needs to have `ps` jobs.")
    self._variable_device = device_setter.replica_device_setter(
        ps_tasks=num_ps_replicas,
        worker_device=self._worker_device,
        merge_devices=True,
        cluster=cluster_spec)

    # The `_parameter_devices` is needed for the `parameter_devices` property
    # and is a list of all variable devices. Here parameter devices are all
    # tasks of the "ps" job.
    self._parameter_devices = map("/job:ps/task:{}".format,
                                  range(num_ps_replicas))

    # Add a default device so that ops without specified devices will not end up
    # on other workers.
    self._default_device = self._worker_device

    self._is_chief = multi_worker_util.is_chief(cluster_spec, task_type,
                                                task_id)
    self._cluster_spec = cluster_spec
    self._task_type = task_type
    self._task_id = task_id

    logging.info(
        "Multi-worker ParameterServerStrategy with "
        "cluster_spec = %r, task_type = %r, task_id = %r, "
        "num_ps_replicas = %r, is_chief = %r, compute_devices = %r, "
        "variable_device = %r", cluster_spec.as_dict(), task_type, task_id,
        num_ps_replicas, self._is_chief, self._compute_devices,
        self._variable_device)

  def _initialize_local(self, num_gpus_per_worker):
    """Initialize internal devices for local training."""
    # Define compute devices which is a list of device strings and one for each
    # tower. When there are GPUs, replicate operations on these GPUs. Otherwise,
    # place operations on CPU.
    if num_gpus_per_worker > 0:
      self._compute_devices = list(
          map("/device:GPU:{}".format, range(num_gpus_per_worker)))
    else:
      self._compute_devices = [_LOCAL_CPU]

    self._compute_devices = list(
        map(device_util.resolve, self._compute_devices))
    self._canonical_compute_device_set = set(self._compute_devices)

    # If there is only one GPU, put everything on that GPU. Otherwise, place
    # variables on CPU.
    if num_gpus_per_worker == 1:
      assert len(list(self._compute_devices)) == 1
      self._variable_device = _LOCAL_GPU_0
      self._parameter_devices = [_LOCAL_GPU_0]
    else:
      self._variable_device = _LOCAL_CPU
      self._parameter_devices = [_LOCAL_CPU]

    self._is_chief = True
    self._cluster_spec = None
    self._task_type = None
    self._task_id = None

    logging.info(
        "ParameterServerStrategy with compute_devices = %r, "
        "variable_device = %r", self._compute_devices, self._variable_device)

  def distribute_dataset(self, dataset_fn):
    """Distributes the dataset to each local GPU."""
    return values.PerDeviceDataset(
        self._call_dataset_fn(dataset_fn), self._compute_devices, True)

  def _broadcast(self, tensor, destinations):
    if not cross_tower_ops_lib.check_destinations(destinations):
      destinations = self._compute_devices
    return self._cross_tower_ops.broadcast(tensor, destinations)

  # TODO(yuefengz): not all ops in device_setter.STANDARD_PS_OPS will go through
  # this creator, such as "MutableHashTable".
  def _create_variable(self, next_creator, *args, **kwargs):
    if self.num_towers > 1:
      aggregation = kwargs.pop("aggregation", vs.VariableAggregation.NONE)
      if aggregation not in (
          vs.VariableAggregation.NONE,
          vs.VariableAggregation.SUM,
          vs.VariableAggregation.MEAN,
          vs.VariableAggregation.ONLY_FIRST_TOWER
      ):
        raise ValueError("Invalid variable aggregation mode: " + aggregation +
                         " for variable: " + kwargs["name"])

      def var_creator(*args, **kwargs):
        # Record what collections this variable should be added to.
        collections = kwargs.pop("collections", None)
        if collections is None:
          collections = [ops.GraphKeys.GLOBAL_VARIABLES]
        kwargs["collections"] = []

        # Create and wrap the variable.
        v = next_creator(*args, **kwargs)
        wrapped = values.AggregatingVariable(v, aggregation)

        # Add the wrapped variable to the requested collections.
        # The handling of eager mode and the global step matches
        # ResourceVariable._init_from_args().
        if not context.executing_eagerly():
          g = ops.get_default_graph()
          # If "trainable" is True, next_creator() will add the contained
          # variable to the TRAINABLE_VARIABLES collection, so we manually
          # remove it and replace with the wrapper. We can't set "trainable"
          # to False for next_creator() since that causes functions like
          # implicit_gradients to skip those variables.
          if kwargs.get("trainable", True):
            collections.append(ops.GraphKeys.TRAINABLE_VARIABLES)
            l = g.get_collection_ref(ops.GraphKeys.TRAINABLE_VARIABLES)
            l.remove(v)
          g.add_to_collections(collections, wrapped)
        elif ops.GraphKeys.GLOBAL_STEP in collections:
          ops.add_to_collections(ops.GraphKeys.GLOBAL_STEP, wrapped)

        return wrapped
    else:
      var_creator = next_creator

    if "colocate_with" in kwargs:
      with ops.device(None):
        with ops.colocate_with(kwargs["colocate_with"]):
          return var_creator(*args, **kwargs)

    with ops.colocate_with(None, ignore_existing=True):
      with ops.device(self._variable_device):
        return var_creator(*args, **kwargs)

  def _call_for_each_tower(self, fn, *args, **kwargs):
    # pylint: disable=protected-access
    return mirrored_strategy._call_for_each_tower(self, fn, *args, **kwargs)

  def _verify_destinations_not_different_worker(self, destinations):
    if destinations is None:
      return
    for d in cross_tower_ops_lib.get_devices_from(destinations):
      d_spec = tf_device.DeviceSpec.from_string(d)
      if d_spec.job == self._task_type and d_spec.task != self._task_id:
        raise ValueError(
            "Cannot reduce to another worker: %r, current worker is %r" %
            (d, self._worker_device))

  def _reduce(self, aggregation, value, destinations):
    self._verify_destinations_not_different_worker(destinations)
    if not isinstance(value, values.DistributedValues):
      # pylint: disable=protected-access
      return mirrored_strategy._reduce_non_distributed_value(
          self, aggregation, value, destinations)
    if aggregation == vs.VariableAggregation.ONLY_FIRST_TOWER:
      return self.broadcast(value.get(self._compute_devices[0]), destinations)
    return self._cross_tower_ops.reduce(
        aggregation, value, destinations=destinations)

  def _batch_reduce(self, aggregation, value_destination_pairs):
    if aggregation == vs.VariableAggregation.ONLY_FIRST_TOWER:
      return [self.broadcast(v.get(self._compute_devices[0]), d)
              for v, d in value_destination_pairs]
    for _, destinations in value_destination_pairs:
      self._verify_destinations_not_different_worker(destinations)
    return self._cross_tower_ops.batch_reduce(aggregation,
                                              value_destination_pairs)

  def _select_single_value(self, structured):
    """Select any single values in `structured`."""

    def _select_fn(x):  # pylint: disable=g-missing-docstring
      if isinstance(x, values.Mirrored):
        if len(x.devices) == 1:
          return list(x._index.values())[0]  # pylint: disable=protected-access
        else:
          raise ValueError(
              "You cannot update variable with a Mirrored object with multiple "
              "components %r when using ParameterServerStrategy. You must "
              "specify a single value or a Mirrored with a single value." % x)
      elif isinstance(x, values.PerDevice):
        raise ValueError(
            "You cannot update variable with a PerDevice object %r when using "
            "ParameterServerStrategy. You must specify a single value or a "
            "Mirrored with a single value" % x)
      else:
        return x

    return nest.map_structure(_select_fn, structured)

  def _update(self, var, options, fn, *args, **kwargs):
    if isinstance(var, values.AggregatingVariable):
      var = var.get()
    if not isinstance(var, resource_variable_ops.ResourceVariable):
      raise ValueError(
          "You can not update `var` %r. It must be a Variable." % var)
    should_group = options.pop("grouped")
    assert not options  # Validate that we are processing all of the options.
    with ops.colocate_with(var), distribute_lib.UpdateContext(var.device):
      result = fn(var, *self._select_single_value(args),
                  **self._select_single_value(kwargs))
      if should_group:
        return result
      else:
        return nest.map_structure(self._unwrap, result)

  # TODO(yuefengz): does it need to call _select_single_value?
  def _update_non_slot(self, colocate_with, options, fn, *args, **kwargs):
    should_group = options.pop("grouped")
    assert not options  # Validate that we are processing all of the options.
    with ops.device(
        colocate_with.device), distribute_lib.UpdateContext(colocate_with):
      result = fn(*args, **kwargs)
      if should_group:
        return result
      else:
        return nest.map_structure(self._unwrap, result)

  def _unwrap(self, val):
    if isinstance(val, values.DistributedValues):
      # Return in a deterministic order.
      if set(val.devices) == self._canonical_compute_device_set:
        return [val.get(device=d) for d in self._compute_devices]
      return [val.get(device=d) for d in sorted(val.devices)]
    return [val]

  def value_container(self, val):
    return values.value_container(val)

  def read_var(self, var):
    # No need to distinguish between normal variables and tower-local variables.
    return array_ops.identity(var)

  def configure(self,
                session_config=None,
                cluster_spec=None,
                task_type=None,
                task_id=None):
    """Configures the strategy class.

    The strategy object will be re-initialized if `cluster_spec` is given but
    was not passed in the constructor.

    Args:
      session_config: not used currently.
      cluster_spec: a dict, ClusterDef or ClusterSpec object specifying the
        cluster configurations.
      task_type: the current task type.
      task_id: the current task id.

    Raises:
      ValueError: if `cluster_spec` is given but `task_type` or `task_id` is
        not.
    """
    if not self._cluster_spec and cluster_spec:
      # If a `cluster_spec` is already passed in, do nothing here.
      # TODO(yuefengz): check `cluster_spec` is the same if this object has
      # already been initialized with a `cluster_spec`.
      if task_type is None or task_id is None:
        raise ValueError("When `cluster_spec` is given, must also specify "
                         "`task_type` and `task_id`.")
      self._cluster_spec = multi_worker_util.normalize_cluster_spec(
          cluster_spec)
      self._task_type = task_type
      self._task_id = task_id
      self._initialize_multi_worker(self._num_gpus_per_worker,
                                    self._cluster_spec, task_type, task_id)

    if not session_config or not self._cluster_spec:
      return

    session_config.isolate_session_state = False

    assert self._cluster_spec
    assert self._task_type
    assert self._task_id is not None

    # The device filters prevent communication between workers.
    if self._task_type not in ["chief", "worker"]:
      return
    del session_config.device_filters[:]
    session_config.device_filters.extend(
        ["/job:%s/task:%d" % (self._task_type, self._task_id), "/job:ps"])

  @property
  def num_towers(self):
    return len(self._compute_devices)

  @property
  def num_replicas_in_sync(self):
    return len(self._compute_devices)

  @property
  def worker_devices(self):
    # Make a copy to prevent users from accidentally mutating our copy.
    return list(self._compute_devices)

  @property
  def parameter_devices(self):
    return list(self._parameter_devices)

  def non_slot_devices(self, var_list):
    return min(var_list, key=lambda x: x.name)

  @property
  def between_graph(self):
    return True

  @property
  def should_init(self):
    return self._is_chief

  @property
  def should_checkpoint(self):
    return self._is_chief

  @property
  def should_save_summary(self):
    return self._is_chief
