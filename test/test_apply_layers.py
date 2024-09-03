import sys
import os
example_folder = os.path.dirname(os.path.dirname(os.path.abspath(
    sys.argv[0]))) + "/examples"
sys.path.append(example_folder)
from decoder_only_model import DecoderOnlyConfig, DecoderOnlyModel  # type:ignore

import sys
import unittest
from typing import Iterable

import torch

import torch_xla
from torch_xla.experimental.apply_layers import apply_layers

from test_utils import XlaTestCase  # type:ignore


class ApplyLayersTest(XlaTestCase):

  def setUp(self):
    super().setUp()

    self.device = torch_xla.device()

  def test_empty_layers(self):
    layers = []
    input_data = torch.randn(64).to(self.device)
    torch_xla.sync()
    output = apply_layers(layers, input_data.clone())
    super().compareResults(output, input_data, abs_err=0.0001, rel_err=0.01)

  def test_linear_layers(self):
    # We want to apply these layers sequentially
    import torch.nn as nn
    layers = [nn.Linear(64, 64).to(self.device) for _ in range(10)]
    input_data = torch.randn(64).to(self.device)

    from copy import deepcopy
    scan_layers = deepcopy(layers)
    loop_layers = deepcopy(layers)
    torch_xla.sync()

    output = apply_layers(scan_layers, input_data.clone())
    output.sum().backward()

    # Test that the result is the same as for loop.
    loop_output = input_data.clone()
    from copy import deepcopy
    for layer in loop_layers:
      loop_output = layer(loop_output)
    torch_xla.sync()

    super().compareResults(loop_output, output, abs_err=0.0001, rel_err=0.01)

    loop_output.sum().backward()
    torch_xla.sync()

    # Test that the gradients are the same too.
    for layer_scan, layer_loop in zip(scan_layers, loop_layers):
      super().compareResults(
          layer_scan.weight.grad,
          layer_loop.weight.grad,
          abs_err=0.0001,
          rel_err=0.01)
      super().compareResults(
          layer_scan.bias.grad,
          layer_loop.bias.grad,
          abs_err=0.0001,
          rel_err=0.01)

  def test_decoder_model(self):
    # Define a decoder model that composes the decoder model in the example,
    # but adds the ability to run the layers with the `scan` operator.
    class DecoderOnlyModelWithScan(torch.nn.Module):

      def __init__(self, **kwargs):
        super(DecoderOnlyModelWithScan, self).__init__()
        self.decoder = DecoderOnlyModel(**kwargs)

      @property
      def layers(self) -> Iterable[torch.nn.Module]:
        return self.decoder.layers

      def forward(
          self,
          input_ids: torch.Tensor,
      ) -> torch.Tensor:
        return self.decoder.forward(input_ids)

      def forward_scan(
          self,
          input_ids: torch.Tensor,
      ) -> torch.Tensor:
        inputs_embeds = self.decoder.embed_tokens(input_ids)
        # embed positions
        assert isinstance(inputs_embeds, torch.Tensor)
        # decoder layers
        hidden_states = apply_layers(self.decoder.layers, inputs_embeds)
        hidden_states = self.decoder.norm(hidden_states)
        # [B, S, H] -> [B, S, V]
        return self.decoder.output(hidden_states)

    # Make it smaller for fast model run and comparisons.
    config = DecoderOnlyConfig(
        hidden_size=128, intermediate_size=8 * 128, vocab_size=256)
    model = DecoderOnlyModelWithScan(config=config).to(self.device)
    batch_size = 2
    sequence_length = 8

    # Generate random input_ids within the range of the vocabulary size
    input_ids = torch.randint(0, config.vocab_size,
                              (batch_size, sequence_length)).to(self.device)

    from copy import deepcopy
    loop_model = deepcopy(model)
    scan_model = deepcopy(model)
    torch_xla.sync()

    # Run the loop-based model.
    loop_output = loop_model(input_ids.clone())
    loop_output.sum().backward()
    torch_xla.sync()

    # Run again, this time using `scan`
    scan_output = scan_model.forward_scan(input_ids.clone())
    scan_output.sum().backward()
    torch_xla.sync()

    # Compare results
    super().compareResults(scan_output, loop_output, abs_err=0.05, rel_err=0.01)

    # Check gradients
    for layer_scan, layer_loop in zip(scan_model.layers, loop_model.layers):
      for (name,
           param_scan), (name2,
                         param_loop) in zip(layer_scan.named_parameters(),
                                            layer_loop.named_parameters()):
        assert name == name2
        if param_scan.grad is not None or param_loop.grad is not None:
          super().compareResults(
              param_scan.grad, param_loop.grad, abs_err=0.1, rel_err=0.05)
          print(f"Pass: {name} {param_scan.shape}")


if __name__ == '__main__':
  test = unittest.main()
  sys.exit(0 if test.result.wasSuccessful() else 1)
