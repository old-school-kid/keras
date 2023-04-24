import collections

from tensorflow import nest

from keras_core.backend import KerasTensor
from keras_core.operations.operation import Operation


class Function(Operation):
    def __init__(self, inputs, outputs, name=None):
        super().__init__(name=name)

        self._inputs_struct = nest.map_structure(lambda x: x, inputs)
        self._outputs_struct = nest.map_structure(lambda x: x, outputs)
        self._inputs = nest.flatten(inputs)
        self._outputs = nest.flatten(outputs)

        (nodes, nodes_by_depth, operations, operations_by_depth) = map_graph(
            self._inputs, self._outputs
        )
        self._nodes = nodes
        self._nodes_by_depth = nodes_by_depth
        self._operations = operations
        self._operations_by_depth = operations_by_depth

    @property
    def inputs(self):
        return self._inputs

    @property
    def outputs(self):
        return self._outputs

    def compute_output_spec(self, inputs):
        self._assert_input_compatibility(inputs)
        # Check if input shapes are identical to ref input shapes,
        # if so take a shortcut.
        shortcut = True
        for x, x_ref in zip(nest.flatten(inputs), self._inputs):
            if x.shape != x_ref.shape:
                shortcut = False
                break
        if shortcut:
            return nest.map_structure(
                lambda x: KerasTensor(shape=x.shape, dtype=x.dtype),
                self._outputs_struct,
            )
        # No luck; take the long road through the graph.
        # Original Keras used a cache to avoid recomputing all this
        # when known input shapes where seen again. Perhaps a good
        # idea to bring that back.
        return self._run_through_graph(
            inputs, operation_fn=lambda op: op.compute_output_spec
        )

    def call(self, inputs):
        """Computes output tensors for new inputs."""
        self._assert_input_compatibility(inputs)
        return self._run_through_graph(inputs, operation_fn=lambda op: op)

    def _run_through_graph(self, inputs, operation_fn):
        """Execute the graph.

        At each node we compute outputs via
        `operation_fn(node.operation)(*args, **kwargs)`.
        """
        inputs = nest.flatten(inputs)

        # Dictionary mapping reference tensors to computed tensors.
        tensor_dict = {}
        for x, y in zip(self.inputs, inputs):
            tensor_dict[x] = y

        nodes_by_depth = self._nodes_by_depth
        depth_keys = list(nodes_by_depth.keys())
        depth_keys.sort(reverse=True)

        for depth in depth_keys:
            nodes = nodes_by_depth[depth]
            for node in nodes:
                if not node.operation or node.is_input:
                    continue  # Input tensors already exist.

                if any(x not in tensor_dict for x in node.input_tensors):
                    continue  # Node is not computable, try skipping.

                args, kwargs = node.arguments.fill_in(tensor_dict)
                outputs = operation_fn(node.operation)(*args, **kwargs)

                # Update tensor_dict.
                for x, y in zip(node.outputs, nest.flatten(outputs)):
                    tensor_dict[x] = y

        output_tensors = []
        for x in self.outputs:
            output_tensors.append(tensor_dict[x])

        return nest.pack_sequence_as(self._outputs_struct, output_tensors)

    def get_config(self):
        # TODO(fchollet)
        raise NotImplementedError

    @classmethod
    def from_config(self, config):
        # TODO(fchollet)
        raise NotImplementedError

    def _assert_input_compatibility(self, inputs):
        try:
            nest.assert_same_structure(
                inputs, self._inputs_struct, check_types=False
            )
        except ValueError:
            raise ValueError(
                "Function was called with an invalid input structure. "
                f"Expected input structure: {self._inputs_struct}\n"
                f"Received input structure: {inputs}"
            )
        for x, x_ref in zip(nest.flatten(inputs), self._inputs):
            if len(x.shape) != len(x_ref.shape):
                raise ValueError(
                    f"{self.__class__.__name__} was passed incompatible inputs. "
                    f"For input '{x_ref.name}', expected shape {x_ref.shape}, "
                    f"but received instead a tensor with shape {x.shape}."
                )
            for dim, ref_dim in zip(x.shape, x_ref.shape):
                if ref_dim is not None and dim is not None:
                    if dim != ref_dim:
                        raise ValueError(
                            f"{self.__class__.__name__} was passed incompatible inputs. "
                            f"For input '{x_ref.name}', expected shape {x_ref.shape}, "
                            f"but received instead a tensor with shape {x.shape}."
                        )


def make_node_key(op_name, node_index):
    return op_name + "_ib-" + str(node_index)


def map_graph(inputs, outputs):
    """Validates a graph's topology and gather its operations and nodes.

    Args:
        inputs: List of input tensors.
        outputs: List of outputs tensors.

    Returns:
        A tuple `(nodes, nodes_by_depth, operations, operations_by_depth)`.
        - nodes: list of Node instances.
        - nodes_by_depth: dict mapping ints (depth) to lists of node instances.
        - operations: list of Operation instances.
        - operations_by_depth: dict mapping ints (depth) to lists of Operation instances.
    """
    # "depth" is number of operations between output Node and the Node.
    # Nodes are ordered from inputs -> outputs.
    nodes_in_decreasing_depth, operation_indices = _build_map(outputs)
    network_nodes = {
        make_node_key(
            str(id(node.operation)), node.operation._inbound_nodes.index(node)
        )
        for node in nodes_in_decreasing_depth
    }

    nodes_depths = {}  # dict {node: depth value}
    operations_depths = {}  # dict {operation: depth value}

    for node in reversed(nodes_in_decreasing_depth):
        # If the depth is not set, the node has no outbound nodes (depth 0).
        depth = nodes_depths.setdefault(node, 0)

        # Update the depth of the corresponding operation
        previous_depth = operations_depths.get(node.operation, 0)
        # If we've seen this operation before at a higher depth,
        # we should use that depth instead of the node depth.
        # This is necessary for shared operations that have inputs at different
        # depth levels in the graph.
        depth = max(depth, previous_depth)
        operations_depths[node.operation] = depth
        nodes_depths[node] = depth

        # Update the depth of inbound nodes.
        # The "depth" of a node is the max of the depths
        # of all nodes it is connected to + 1.
        for node_dep in node.parent_nodes:
            previous_depth = nodes_depths.get(node_dep, 0)
            nodes_depths[node_dep] = max(depth + 1, previous_depth)

    # Handle inputs that are not connected to outputs.
    # We do not error out here because the inputs may be used to compute losses
    # and metrics.
    for input_t in inputs:
        input_operation = input_t._keras_history[0]
        if input_operation and input_operation not in operations_depths:
            operations_depths[input_operation] = 0
            operation_indices[input_operation] = -1
            nodes_depths[input_operation._inbound_nodes[0]] = 0
            network_nodes.add(make_node_key(input_operation.name, 0))

    # Build a dict {depth: list of nodes with this depth}
    nodes_by_depth = collections.defaultdict(list)
    for node, depth in nodes_depths.items():
        nodes_by_depth[depth].append(node)

    # Build a dict {depth: list of operations with this depth}
    operations_by_depth = collections.defaultdict(list)
    for operation, depth in operations_depths.items():
        operations_by_depth[depth].append(operation)

    # Get sorted list of operation depths.
    depth_keys = list(operations_by_depth.keys())
    depth_keys.sort(reverse=True)

    # Set self.operations ordered by depth.
    operations = []
    for depth in depth_keys:
        operations_for_depth = operations_by_depth[depth]
        # Network.operations needs to have a deterministic order:
        # here we order them by traversal order.
        operations_for_depth.sort(key=lambda x: operation_indices[x])
        operations.extend(operations_for_depth)

    # Get sorted list of node depths.
    depth_keys = list(nodes_by_depth.keys())
    depth_keys.sort(reverse=True)

    # Check that all tensors required are computable.
    # computable_tensors: all tensors in the graph
    # that can be computed from the inputs provided.
    computable_tensors = set()
    for x in inputs:
        computable_tensors.add(x)

    operations_with_complete_input = []  # To provide a better error msg.
    for depth in depth_keys:
        for node in nodes_by_depth[depth]:
            for x in nest.flatten(node.input_tensors):
                if x not in computable_tensors:
                    operation = node.operation
                    raise ValueError(
                        "Graph disconnected: cannot find parent for "
                        f"tensor {x} at operation '{operation}'. "
                        "The following previous operations were accessed "
                        f"without issue: {operations_with_complete_input}"
                    )
                operations_with_complete_input.append(operation.name)

            for x in nest.flatten(node.outputs):
                computable_tensors.add(x)

    # Ensure name unicity, which will be crucial for serialization
    # (since serialized nodes refer to operations by their name).
    all_names = [operation.name for operation in operations]
    for name in all_names:
        if all_names.count(name) != 1:
            raise ValueError(
                f'The name "{name}" is used {all_names.count(name)} '
                "times in the model. All operation names should be unique."
            )
    return network_nodes, nodes_by_depth, operations, operations_by_depth


def _build_map(outputs):
    """Topologically sort nodes in order from inputs to outputs.

    It uses a depth-first search to topologically sort nodes that appear in the
    _keras_history connectivity metadata of `outputs`.

    Args:
        outputs: the output tensors whose _keras_history metadata should be
                walked. This may be an arbitrary nested structure.

    Returns:
        A tuple like (ordered_nodes, operation_to_first_traversal_index)
        ordered_nodes: list of nodes appearing in the keras history, topologically
            sorted from original inputs to the `outputs`.
            (If outputs have different sets of ancestors, the inputs to one output
            may appear after a different output).
        operation_to_first_traversal_index:
            A dict mapping operation to the traversal index in the DFS where it is
            seen. Note: if a operation is shared by several nodes, the dict will only
            store the index corresponding to the *first* time the operation seen.
    """
    finished_nodes = set()
    nodes_in_progress = set()
    nodes_in_decreasing_depth = []  # nodes from inputs -> outputs.
    operation_indices = {}  # operation -> in traversal order.
    for output in nest.flatten(outputs):
        _build_map_helper(
            output,
            finished_nodes,
            nodes_in_progress,
            nodes_in_decreasing_depth,
            operation_indices,
        )
    return nodes_in_decreasing_depth, operation_indices


def _build_map_helper(
    tensor,
    finished_nodes,
    nodes_in_progress,
    nodes_in_decreasing_depth,
    operation_indices,
):
    """Recursive helper for `_build_map`."""
    (
        operation,
        node_index,
        _,
    ) = tensor._keras_history
    if not operation:
        return

    node = operation._inbound_nodes[node_index]

    # Don't repeat work for shared subgraphs
    if node in finished_nodes:
        return

    # Prevent cycles.
    if node in nodes_in_progress:
        raise ValueError(
            f'Tensor {tensor} from operation "{operation.name}" is part of a cycle.'
        )

    # Store the traversal order for operation sorting.
    if operation not in operation_indices:
        operation_indices[operation] = len(operation_indices)

    # Propagate to all previous tensors connected to this node.
    nodes_in_progress.add(node)
    if not node.is_input:
        for tensor in node.input_tensors:
            _build_map_helper(
                tensor,
                finished_nodes,
                nodes_in_progress,
                nodes_in_decreasing_depth,
                operation_indices,
            )

    finished_nodes.add(node)
    nodes_in_progress.remove(node)
    nodes_in_decreasing_depth.append(node)