#!/usr/bin/python
#
# Copyright 2016 Google Inc. All rights reserved
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import re
import sys

# Whitelists for Android
# TODO: Move to an external file?

INPUT_WHITELIST = [
    # This is created at ckati-time.
    r'out/build_number\.txt',
    r'out/build_date\.txt',
    # Currently, Android build system doesn't specify shared objects
    # in prebuilts/ for example.
    # TODO: Probably need a further investigation.
    r'prebuilts/',
    # Python automatically creates them.
    r'.*\.pyc$',
    # droiddoc uses these HTML files.
    # TODO: Probably not so important, but maybe better to fix.
    r'.*/package\.html$',
]
INPUT_WHITELIST = set(os.path.join(os.getcwd(), p) for p in INPUT_WHITELIST)
INPUT_WHITELIST_RE = re.compile('|'.join(INPUT_WHITELIST))

OUTPUT_WHITELIST = [
    r'out/host/common/obj/PACKAGING/gpl_source_intermediates/gpl_source\.tgz',
    # Per discussion in https://android-review.googlesource.com/#/c/224070/
    r'out/target/product/generic[^/]*/aosp_\w+-symbols-\w+.\w+\.zip',
]
OUTPUT_WHITELIST = set(os.path.join(os.getcwd(), p) for p in OUTPUT_WHITELIST)
OUTPUT_WHITELIST_RE = re.compile('|'.join(OUTPUT_WHITELIST))

class DepSanitizer(object):
  def __init__(self, dsan_dir):
    self.dsan_dir = dsan_dir
    self.outputs = {}
    self.defaults = []
    self.checked = {}
    self.cwd = os.getcwd()
    self.has_error = False
    self.is_verbose = False

  def add_node(self, output, rule, inputs, depfile, is_restat):
    assert output not in self.outputs
    self.outputs[output] = (rule, inputs, depfile, is_restat)

  def set_defaults(self, defaults):
    assert not self.defaults
    self.defaults = defaults

  def set_is_verbose(self, is_verbose):
    self.is_verbose = is_verbose

  def run(self):
    for o in self.defaults:
      self.check_dep_rec(o)

  def read_inputs_from_depfile(self, rule, depfile):
    # TODO: Comment out this.
    if not os.path.exists(depfile):
      print '%s: %s file not exists!' % (rule, depfile)
      self.has_error = True
      return []

    r = []
    with open(depfile) as f:
      for tok in f.read().split():
        if tok.endswith(':') or tok == '\\':
          continue
        # Android specific - files in out directories should be
        # explicitly defined as inputs.
        if tok.startswith('out/'):
          continue
        r.append(tok)
    return r

  def check_dep_rec(self, output):
    if output not in self.outputs:
      # Leaf node.
      return set((output,))

    if output in self.checked:
      return self.checked[output]
    # TODO: Why do we need this?
    self.checked[output] = set()

    rule, inputs, depfile, is_restat = self.outputs[output]
    if depfile:
      inputs += self.read_inputs_from_depfile(rule, depfile)

    products = set()
    for input in inputs:
      products |= self.check_dep_rec(input)

    actual_outputs = set()
    if rule != 'phony':
      if is_restat:
        # A build recipe with restat usually reads the output
        # first and causes false positives for incremental build.
        # TODO: Add a flag which specifies a build was an incremental
        # build or a full build?
        actual_outputs = set((output,))
      else:
        actual_outputs = self.check_dep(output, rule, inputs, products)

    r = products | actual_outputs
    self.checked[output] = r
    return r

  def parse_trace_file(self, err_prefix, fn):
    # TODO: Comment out this.
    if not os.path.exists(fn):
      print '%s: %s file not exists!' % (err_prefix, fn)
      self.has_error = True
      return set(), set()

    actual_inputs = set()
    actual_outputs = set()
    with open(fn) as f:
      a = actual_inputs
      for line in f:
        line = line.strip()
        if line == '':
          a = actual_outputs
        elif line == 'TIMED OUT':
          print '%s: timed out - diagnostics will be incomplete' % err_prefix
        else:
          a.add(line)
    return actual_inputs, actual_outputs

  def check_dep(self, output, rule, inputs, products):
    has_error = False
    err_prefix = '%s(%s)' % (rule, output)

    fn = os.path.join(self.dsan_dir, output.replace('/', '__') + '.trace')
    actual_inputs, actual_outputs = self.parse_trace_file(err_prefix, fn)

    output = os.path.abspath(os.path.join(self.cwd, output))
    inputs = set(os.path.abspath(os.path.join(self.cwd, i)) for i in inputs)

    if output not in actual_outputs:
      print '%s: should not have %s as the output' % (err_prefix, output)
      has_error = True

    undefined_inputs = actual_inputs - inputs - products
    if OUTPUT_WHITELIST_RE.match(output):
      undefined_inputs = set()
    for undefined_input in undefined_inputs:
      if not undefined_input.startswith(self.cwd):
        continue

      if INPUT_WHITELIST_RE.match(undefined_input):
        continue
      # Ninja's rspfile.
      if undefined_input == output + '.rsp':
        continue

      if os.path.isdir(undefined_input):
        continue
      print '%s: should have %s as an input' % (err_prefix, undefined_input)
      has_error = True

    if has_error:
      self.has_error = True
      if self.is_verbose:
        print '%s: inputs: %s' % (err_prefix, inputs | products)

    return actual_inputs | inputs | actual_outputs


def unescape_ninja_dollar(m):
  c = m.group(0)[1]
  if c == '$' or c == ':' or c == ' ':
    return c
  raise Exception("not supported yet: %s" % m.group(0))


def unescape_ninja(l):
  return re.subn(r'\$.', unescape_ninja_dollar, l)[0]


args = list(sys.argv)

is_verbose = False
if args[1] == '-v':
  args.pop(1)
  is_verbose = True

if len(args) != 3:
  print('Usage: %s dsandir build.ninja' % sys.argv[0])
  sys.exit(1)

dsan = DepSanitizer(args[1])
dsan.set_is_verbose(is_verbose)

depfile = None
is_restat = False
defaults = None
sys.stderr.write('Parsing %s...\n' % args[2])
with open(args[2]) as f:
  for line in f:
    line = line.rstrip()
    if line.startswith('build '):
      line = unescape_ninja(line)
      toks = line.split(' ')
      output = toks[1][0:-1]
      rule = toks[2]
      inputs = toks[3:]
      dsan.add_node(output, rule, inputs, depfile, is_restat)
      depfile = None
      is_restat = False
    elif line.startswith('default '):
      line = unescape_ninja(line)
      assert not defaults
      defaults = line.split(' ')[1:]
    elif line.startswith(' depfile = '):
      line = unescape_ninja(line)
      assert not depfile
      depfile = line.split(' ')[3]
    elif line.startswith(' restat = 1'):
      is_restat = True
      pass

assert defaults
dsan.set_defaults(defaults)

sys.stderr.write('Analyzing dependency...\n')
dsan.run()

if dsan.has_error:
  sys.exit(1)
