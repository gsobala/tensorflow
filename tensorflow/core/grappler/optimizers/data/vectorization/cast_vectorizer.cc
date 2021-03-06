/* Copyright 2018 The TensorFlow Authors. All Rights Reserved.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
==============================================================================*/

#include "tensorflow/core/framework/node_def.pb.h"
#include "tensorflow/core/grappler/optimizers/data/graph_utils.h"
#include "tensorflow/core/grappler/optimizers/data/vectorization/vectorizer_registry.h"

namespace tensorflow {
namespace grappler {
namespace {

class CastVectorizer : public Vectorizer {
 public:
  Status Vectorize(const Node& node, Graph* outer_scope,
                   std::vector<WrappedTensor>&& inputs,
                   std::vector<WrappedTensor>* outputs) override {
    Status s;
    if (node.num_inputs() != 1) {
      return errors::Internal("Cast op should only have one input.");
    }

    // Add new Cast node with the same op and attrs as the original node
    auto new_cast_node = outer_scope->AddNode(node.def(), &s);
    TF_RETURN_IF_ERROR(s);

    outer_scope->AddEdge(inputs[0].node, inputs[0].output_index, new_cast_node,
                         0);

    // Add output mappings
    outputs->push_back({new_cast_node, 0, true});
    return Status::OK();
  }
};

REGISTER_VECTORIZER("Cast", CastVectorizer);

}  // namespace
}  // namespace grappler
}  // namespace tensorflow
