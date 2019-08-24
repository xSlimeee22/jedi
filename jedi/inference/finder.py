"""
Searching for names with given scope and name. This is very central in Jedi and
Python. The name resolution is quite complicated with descripter,
``__getattribute__``, ``__getattr__``, ``global``, etc.

If you want to understand name resolution, please read the first few chapters
in http://blog.ionelmc.ro/2015/02/09/understanding-python-metaclasses/.

Flow checks
+++++++++++

Flow checks are not really mature. There's only a check for ``isinstance``.  It
would check whether a flow has the form of ``if isinstance(a, type_or_tuple)``.
Unfortunately every other thing is being ignored (e.g. a == '' would be easy to
check for -> a is a string). There's big potential in these checks.
"""

from parso.python import tree
from parso.tree import search_ancestor
from jedi import debug
from jedi import settings
from jedi.inference.arguments import TreeArguments
from jedi.inference import helpers
from jedi.inference.value import iterable
from jedi.inference.base_value import ValueSet, NO_VALUES
from jedi.parser_utils import is_scope, get_parent_scope


class NameFinder(object):
    def __init__(self, context, name_context, name_or_str,
                 position=None, analysis_errors=True):
        # Make sure that it's not just a syntax tree node.
        self._context = context
        self._name_context = name_context
        self._name = name_or_str
        if isinstance(name_or_str, tree.Name):
            self._string_name = name_or_str.value
        else:
            self._string_name = name_or_str
        self._position = position
        self._analysis_errors = analysis_errors

    def find(self, names, attribute_lookup):
        """
        :params bool attribute_lookup: Tell to logic if we're accessing the
            attribute or the contents of e.g. a function.
        """
        values = ValueSet.from_sets(name.infer() for name in names)
        debug.dbg('finder._names_to_types: %s -> %s', names, values)
        if values:
            return values
        return self._add_other_knowledge()

    def filter_name(self, filters):
        """
        Searches names that are defined in a scope (the different
        ``filters``), until a name fits.
        """
        names = []
        for filter in filters:
            names = filter.get(self._string_name)
            if names:
                break

        return list(names)

    def _add_other_knowledge(self):
        # Add isinstance and other if/assert knowledge.
        if isinstance(self._name, tree.Name) and \
                not self._name_context.is_instance() and not self._context.is_compiled():
            flow_scope = self._name
            base_nodes = [self._name_context.tree_node]

            if any(b.type in ('comp_for', 'sync_comp_for') for b in base_nodes):
                return NO_VALUES
            while True:
                flow_scope = get_parent_scope(flow_scope, include_flows=True)
                n = _check_flow_information(self._name_context, flow_scope,
                                            self._name, self._position)
                if n is not None:
                    return n
                if flow_scope in base_nodes:
                    break
        return NO_VALUES


def _check_flow_information(value, flow, search_name, pos):
    """ Try to find out the type of a variable just with the information that
    is given by the flows: e.g. It is also responsible for assert checks.::

        if isinstance(k, str):
            k.  # <- completion here

    ensures that `k` is a string.
    """
    if not settings.dynamic_flow_information:
        return None

    result = None
    if is_scope(flow):
        # Check for asserts.
        module_node = flow.get_root_node()
        try:
            names = module_node.get_used_names()[search_name.value]
        except KeyError:
            return None
        names = reversed([
            n for n in names
            if flow.start_pos <= n.start_pos < (pos or flow.end_pos)
        ])

        for name in names:
            ass = search_ancestor(name, 'assert_stmt')
            if ass is not None:
                result = _check_isinstance_type(value, ass.assertion, search_name)
                if result is not None:
                    return result

    if flow.type in ('if_stmt', 'while_stmt'):
        potential_ifs = [c for c in flow.children[1::4] if c != ':']
        for if_test in reversed(potential_ifs):
            if search_name.start_pos > if_test.end_pos:
                return _check_isinstance_type(value, if_test, search_name)
    return result


def _check_isinstance_type(value, element, search_name):
    try:
        assert element.type in ('power', 'atom_expr')
        # this might be removed if we analyze and, etc
        assert len(element.children) == 2
        first, trailer = element.children
        assert first.type == 'name' and first.value == 'isinstance'
        assert trailer.type == 'trailer' and trailer.children[0] == '('
        assert len(trailer.children) == 3

        # arglist stuff
        arglist = trailer.children[1]
        args = TreeArguments(value.inference_state, value, arglist, trailer)
        param_list = list(args.unpack())
        # Disallow keyword arguments
        assert len(param_list) == 2
        (key1, lazy_value_object), (key2, lazy_value_cls) = param_list
        assert key1 is None and key2 is None
        call = helpers.call_of_leaf(search_name)
        is_instance_call = helpers.call_of_leaf(lazy_value_object.data)
        # Do a simple get_code comparison. They should just have the same code,
        # and everything will be all right.
        normalize = value.inference_state.grammar._normalize
        assert normalize(is_instance_call) == normalize(call)
    except AssertionError:
        return None

    value_set = NO_VALUES
    for cls_or_tup in lazy_value_cls.infer():
        if isinstance(cls_or_tup, iterable.Sequence) and cls_or_tup.array_type == 'tuple':
            for lazy_value in cls_or_tup.py__iter__():
                value_set |= lazy_value.infer().execute_with_values()
        else:
            value_set |= cls_or_tup.execute_with_values()
    return value_set
