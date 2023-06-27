"""
interactive API

`Interactive` is a wrapper around a Python object that lets users create
interactive pipelines by calling existing APIs on an object with dynamic
parameters or widgets.

An `Interactive` instance watches what operations are applied to the
object and records these on each instance, which are then strung
together into a chain.

The original input to an interactive pipeline is stored in a mutable
list and can be accessed via the `_obj` property. The shared mutable
data structure ensures that all `Interactive` instances created from
the same object can hold a shared reference that can be updated,
e.g. via the `.set` method or because the input was itself a reference
to some object that can potentially be updated.

When an operation is applied to an `Interactive` instance, it will
record the operation and create a new instance using `_clone` method,
e.g. `dfi.head()` first records that the `'head'` attribute is
accessed, this is achieved by overriding `__getattribute__`. A new
interactive object is returned, which will then record that it is
being called, and that new object will be itself called as
`Interactive` implements `__call__`. `__call__` returns another
`Interactive` instance. To be able to watch all the potential
operations that may be applied to an object, `Interactive` implements:

- `__getattribute__`: Watching for attribute accesses
- `__call__`: Intercepting both actual calls or method calls if an
  attribute was previously accessed
- `__getitem__`: Intercepting indexing operations
- Operators: Implementing all valid operators `__gt__`, `__add__`, etc.
- `__array_ufunc__`: Intercepting numpy universal function calls

The `interactive` object evaluates operations lazily but whenever the
current value is needed the operations are automatically
evaluated. Note that even attribute access or tab-completion
operations can result in evaluation of the pipeline. This is very
useful in Notebook sessions, as this allows to inspect the operationed
object at any point of the pipeline, and as such provide correct
auto-completion and docstrings. E.g. executing `dfi.A.max?` in an
interactive REPL or notebook where it allows returning the docstring
of the method being accessed.

The actual operations are stored as a dictionary on the `_operation`
attribute of each instance. They contain 4 keys:

- `fn`: The function to apply (either an actual function or a string
        indicating the operation is a method on the object)
- `args`: Any arguments to supply to the `fn`.
- `kwargs`: Any keyword arguments to supply to the `fn`.
- `reverse`: If the function is not a method this indicates whether
             the first arg and the input object should be supplied in
             reverse order.

The `_depth` attribute starts at 0 and is incremented by 1 every time
a new `Interactive` instance is created part of a chain.  The root
instance in an expression has a `_depth` of 0. An expression can
consist of multiple chains, such as `dfi[dfi.A > 1]`, as the
`Interactive` instance is referenced twice in the expression. As a
consequence `_depth` is not the total count of `Interactive` instance
creations of a pipeline, it is the count of instances created in the
outer chain. In the example, that would be `dfi[]`. Each `Interactive`
instance keeps a reference to the previous instance in the chain and
each instance tracks whether its current value is up-to-date via the
`_dirty` attribute which is set to False if any dependency changes.

The `_method` attribute is a string that temporarily stores the
method/attr accessed on the object, e.g. `_method` is 'head' in
`dfi.head()`, until the Interactive instance created in the pipeline
is called at which point `_method` is reset to None. In cases such as
`dfi.head` or `dfi.A`, `_method` is not (yet) reset to None. At this
stage the Interactive instance returned has its `_current` attribute
not updated, e.g. `dfi.A._current` is still the original dataframe,
not the 'A' series. Keeping `_method` is thus useful for instance to
display `dfi.A`, as the evaluation of the object will check whether
`_method` is set or not, and if it's set it will use it to compute the
object returned, e.g. the series `df.A` or the method `df.head`, and
display its repr.
"""
import math
import operator
import sys

from types import FunctionType, MethodType

import param

from .depends import (
    bind, depends, register_depends_transform, transform_dependency,
)
from .layout import Column, HSpacer, Row
from .pane import panel
from .util import eval_function, full_groupby, get_method_owner
from .widgets.base import Widget


def _flatten(line):
    """
    Flatten an arbitrarily nested sequence.

    Inspired by: pd.core.common.flatten

    Parameters
    ----------
    line : sequence
        The sequence to flatten

    Notes
    -----
    This only flattens list, tuple, and dict sequences.

    Returns
    -------
    flattened : generator
    """
    for element in line:
        if any(isinstance(element, tp) for tp in (list, tuple, dict)):
            yield from _flatten(element)
        else:
            yield element

def _find_widgets(op):
    widgets = []
    op_args = list(op['args']) + list(op['kwargs'].values())
    op_args = _flatten(op_args)
    for op_arg in op_args:
        # Find widgets introduced as `widget` in an expression
        if isinstance(op_arg, Widget) and op_arg not in widgets:
            widgets.append(op_arg)
        # Find Ipywidgets
        if 'ipywidgets' in sys.modules:
            from ipywidgets import Widget as IPyWidget
            if isinstance(op_arg, IPyWidget) and op_arg not in widgets:
                widgets.append(op_arg)
        # Find widgets introduced as `widget.param.value` in an expression
        if (isinstance(op_arg, param.Parameter) and
            isinstance(op_arg.owner, Widget) and
            op_arg.owner not in widgets):
            widgets.append(op_arg.owner)
        if isinstance(op_arg, slice):
            nested_op = {"args": [op_arg.start, op_arg.stop, op_arg.step], "kwargs": {}}
            for widget in _find_widgets(nested_op):
                if widget not in widgets:
                    widgets.append(widget)
    return widgets


class Wrapper(param.Parameterized):

    object = param.Parameter()


class interactive_base:
    """
    The `interactive` allows wrapping objects and then operating on
    them interactively while recording any operations applied to them.
    By recording all arguments or operands in the operations the recorded
    pipeline can be replayed if an operand represents a dynamic value.

    Parameters
    ----------
    obj: any
        A supported data structure object

    Examples
    --------
    Instantiate it from an object:

    >>> ifloat = Interactive(3.14)
    >>> ifloat * 2
    6.28

    Then update the original value and see the new result:
    >>> ifloat.set(1)
    2
    """

    @classmethod
    def _applies(cls, obj):
        """
        Subclasses must implement applies and return a boolean to indicate
        whether the subclass should apply or not to the obj.
        """
        return True

    def __new__(cls, obj, **kwargs):
        wrapper = None
        obj = transform_dependency(obj)
        if kwargs.get('fn'):
            fn = kwargs.pop('fn')
            wrapper = kwargs.pop('wrapper', None)
        elif isinstance(obj, (FunctionType, MethodType)):
            fn = obj
            obj = None
        elif isinstance(obj, param.Parameter):
            fn = bind(lambda obj: obj, obj)
            obj = getattr(obj.owner, obj.name)
        elif isinstance(obj, Widget):
            fn = bind(lambda obj: obj, obj)
            obj = obj.value
        else:
            wrapper = Wrapper(object=obj)
            fn = bind(lambda obj: obj, wrapper.param.object)
        clss = cls
        for subcls in cls.__subclasses__():
            if subcls._applies(obj):
                clss = subcls
        inst = super(interactive_base, cls).__new__(clss)
        inst._fn = fn
        inst._shared_obj = kwargs.get('_shared_obj', None if obj is None else [obj])
        inst._wrapper = wrapper
        return inst

    def __init__(
        self, obj, operation=None, fn=None, depth=0, method=None, prev=None,
        _shared_obj=None, _current=None, **kwargs
    ):
        # _init is used to prevent to __getattribute__ to execute its
        # specialized code.
        self._init = False
        self._method = method
        self._operation = operation
        self._depth = depth
        if isinstance(obj, interactive_base) and not prev:
            self._prev = obj
        else:
            self._prev = prev
        self._kwargs = kwargs
        self._init = True
        self._dirty = True
        self._current_ = None
        self._setup_invalidations(depth)

    @property
    def _obj(self):
        if self._shared_obj is None:
            self._obj = eval_function(self._fn)
        return self._shared_obj[0]

    @_obj.setter
    def _obj(self, obj):
        if self._shared_obj is None:
            self._shared_obj = [obj]
        else:
            self._shared_obj[0] = obj

    @property
    def _current(self):
        if self._dirty:
            self.eval()
        return self._current_

    @property
    def _fn_params(self) -> list[param.Parameter]:
        if self._fn is None:
            return []

        owner = get_method_owner(self._fn)
        if owner is not None:
            deps = [
                dep.pobj for dep in owner.param.method_dependencies(self._fn.__name__)
            ]
            return deps

        dinfo = getattr(self._fn, '_dinfo', {})
        args = list(dinfo.get('dependencies', []))
        kwargs = list(dinfo.get('kw', {}).values())
        return args + kwargs

    @property
    def _params(self):
        ps = self._fn_params

        # Collect parameters on previous objects in chain
        prev = self._prev
        while prev is not None:
            for p in prev._params:
                if p not in ps:
                    ps.append(p)
            prev = prev._prev

        if self._operation is None:
            return ps

        # Accumulate dependencies in args and/or kwargs
        for arg in self._operation['args']:
            if isinstance(arg, interactive):
                for p in  arg._params:
                    if p not in ps:
                        ps.append(p)
                continue
            parg = transform_dependency(arg)
            if parg and isinstance(parg, param.Parameter) and parg not in ps:
                ps.append(parg)

        for k, arg in self._operation['kwargs'].items():
            if isinstance(arg, interactive):
                for p in  arg._params:
                    if p not in ps:
                        ps.append(p)
                continue
            parg = transform_dependency(arg)
            if parg is None or k == 'ax' or not isinstance(parg, param.Parameter) or parg in ps:
                continue
            ps.append(parg)
        return ps

    def _setup_invalidations(self, depth=0):
        """
        Since the parameters of the pipeline can change at any time
        we have to invalidate the internal state of the pipeline.
        To handle both invalidations of the inputs of the pipeline
        and the pipeline itself we set up watchers on both.

        1. The first invalidation we have to set up is to re-evaluate
           the function that feeds the pipeline. Only the root node of
           a pipeline has to perform this invalidation because all
           leaf nodes inherit the same shared_obj. This avoids
           evaluating the same function for every branch of the pipeline.
        2. The second invalidation is for the pipeline itself, i.e.
           if any parameter changes we have to notify the pipeline that
           it has to re-evaluate the pipeline. This is done by marking
           the pipeline as `_dirty`. The next time the `_current` value
           is requested we then run and `.eval()` pass that re-executes
           the pipeline.
        """
        if self._fn is not None and depth == 0:
            for _, params in full_groupby(self._fn_params, lambda x: id(x.owner)):
                params[0].owner.param.watch(self._update_obj, [p.name for p in params])
        for _, params in full_groupby(self._params, lambda x: id(x.owner)):
            params[0].owner.param.watch(self._invalidate_current, [p.name for p in params])

    def _invalidate_current(self, *events):
        self._dirty = True

    def _update_obj(self, *args):
        self._obj = eval_function(self._fn)

    @property
    def _callback(self):
        def evaluate_inner():
            return self.eval()
        params = self._params
        if params:
            @depends(*params)
            def evaluate(*args, **kwargs):
                return evaluate_inner()
        else:
            def evaluate():
                return evaluate_inner()
        return evaluate

    def _clone(self, operation=None, copy=False, **kwargs):
        operation = operation or self._operation
        depth = self._depth + 1
        if copy:
            kwargs = dict(
                self._kwargs, _current=self._current, method=self._method, fn=self._fn,
                prev=self._prev, wrapper=self._wrapper, **kwargs
            )
        else:
            kwargs = dict(prev=self, **dict(self._kwargs, **kwargs))
        if kwargs['prev']:
            print(operation, kwargs['prev']._wrapper)
        return type(self)(
            self._obj, operation=operation, depth=depth, _shared_obj=self._shared_obj, **kwargs
        )

    def __dir__(self):
        current = self._current
        if self._method:
            current = getattr(current, self._method)
        extras = {attr for attr in dir(current) if not attr.startswith('_')}
        try:
            return sorted(set(super().__dir__()) | extras)
        except Exception:
            return sorted(set(dir(type(self))) | set(self.__dict__) | extras)

    def _resolve_accessor(self):
        if not self._method:
            # No method is yet set, as in `dfi.A`, so return a copied clone.
            return self._clone(copy=True)
        # This is executed when one runs e.g. `dfi.A > 1`, in which case after
        # dfi.A the _method 'A' is set (in __getattribute__) which allows
        # _resolve_accessor to keep building the operation dim expression.
        operation = {
            'fn': operator.getitem,
            'args': self._method,
            'kwargs': {},
            'reverse': False
        }
        try:
            new = self._clone(operation)
        finally:
            # Reset _method for whatever happens after the accessor has been
            # fully resolved, e.g. whatever happens `dfi.A > 1`.
            self._method = None
        return new

    def __getattribute__(self, name):
        self_dict = super().__getattribute__('__dict__')
        no_lookup = (
            'eval', '_dirty', '_prev', '_operation', '_obj', '_shared_obj',
            '_method', '_eval_operation', '_display_opts', '_fn'
        )
        if not self_dict.get('_init') or name in no_lookup:
            return super().__getattribute__(name)

        current = self_dict['_current_']
        dirty = self_dict['_dirty']
        if dirty:
            self.eval()
            current = self_dict['_current_']

        method = self_dict['_method']
        if method:
            current = getattr(current, method)
        # Getting all the public attributes available on the current object,
        # e.g. `sum`, `head`, etc.
        extras = [d for d in dir(current) if not d.startswith('_')]
        if name in extras and name not in super().__dir__():
            new = self._resolve_accessor()
            # Setting the method name for a potential use later by e.g. an
            # operator or method, as in `dfi.A > 2`. or `dfi.A.max()`
            new._method = name
            try:
                new.__doc__ = getattr(current, name).__doc__
            except Exception:
                pass
            return new
        return super().__getattribute__(name)

    def __call__(self, *args, **kwargs):
        if self._method is None:
            if self._depth == 0:
                # This code path is entered when initializing an interactive
                # class from the accessor, e.g. with df.interactive(). As
                # calling the accessor df.interactive already returns an
                # interactive instance.
                return self._clone(*args, **kwargs)
            # TODO: When is this error raised?
            raise AttributeError
        new = self._clone(copy=True)
        try:
            kwargs = dict(kwargs)
            operation = {
                'fn': new._method,
                'args': args,
                'kwargs': kwargs,
                'reverse': False
            }
            clone = new._clone(operation)
        finally:
            # If an error occurs reset _method anyway so that, e.g. the next
            # attempt in a Notebook, is set appropriately.
            new._method = None
        return clone

    #----------------------------------------------------------------
    # interactive pipeline APIs
    #----------------------------------------------------------------

    def __array_ufunc__(self, ufunc, method, *args, **kwargs):
        new = self._resolve_accessor()
        operation = {
            'fn': getattr(ufunc, method),
            'args': args[1:],
            'kwargs': kwargs,
            'reverse': False
        }
        return new._clone(operation)

    def _apply_operator(self, operator, *args, reverse=False, **kwargs):
        new = self._resolve_accessor()
        operation = {
            'fn': operator,
            'args': args,
            'kwargs': kwargs,
            'reverse': reverse
        }
        return new._clone(operation)

    # Builtin functions

    def __abs__(self):
        return self._apply_operator(abs)

    def __round__(self, ndigits=None):
        args = () if ndigits is None else (ndigits,)
        return self._apply_operator(round, *args)

    # Unary operators
    def __ceil__(self):
        return self._apply_operator(math.ceil)
    def __floor__(self):
        return self._apply_operator(math.floor)
    def __invert__(self):
        return self._apply_operator(operator.inv)
    def __neg__(self):
        return self._apply_operator(operator.neg)
    def __not__(self):
        return self._apply_operator(operator.not_)
    def __pos__(self):
        return self._apply_operator(operator.pos)
    def __trunc__(self):
        return self._apply_operator(math.trunc)

    # Binary operators
    def __add__(self, other):
        return self._apply_operator(operator.add, other)
    def __and__(self, other):
        return self._apply_operator(operator.and_, other)
    def __contains_(self, other):
        return self._apply_operator(operator.contains, other)
    def __divmod__(self, other):
        return self._apply_operator(divmod, other)
    def __eq__(self, other):
        return self._apply_operator(operator.eq, other)
    def __floordiv__(self, other):
        return self._apply_operator(operator.floordiv, other)
    def __ge__(self, other):
        return self._apply_operator(operator.ge, other)
    def __gt__(self, other):
        return self._apply_operator(operator.gt, other)
    def __le__(self, other):
        return self._apply_operator(operator.le, other)
    def __lt__(self, other):
        return self._apply_operator(operator.lt, other)
    def __lshift__(self, other):
        return self._apply_operator(operator.lshift, other)
    def __matmul__(self, other):
        return self._apply_operator(operator.matmul, other)
    def __mod__(self, other):
        return self._apply_operator(operator.mod, other)
    def __mul__(self, other):
        return self._apply_operator(operator.mul, other)
    def __ne__(self, other):
        return self._apply_operator(operator.ne, other)
    def __or__(self, other):
        return self._apply_operator(operator.or_, other)
    def __rshift__(self, other):
        return self._apply_operator(operator.rshift, other)
    def __pow__(self, other):
        return self._apply_operator(operator.pow, other)
    def __sub__(self, other):
        return self._apply_operator(operator.sub, other)
    def __truediv__(self, other):
        return self._apply_operator(operator.truediv, other)
    def __xor__(self, other):
        return self._apply_operator(operator.xor, other)

    # Reverse binary operators
    def __radd__(self, other):
        return self._apply_operator(operator.add, other, reverse=True)
    def __rand__(self, other):
        return self._apply_operator(operator.and_, other, reverse=True)
    def __rdiv__(self, other):
        return self._apply_operator(operator.div, other, reverse=True)
    def __rdivmod__(self, other):
        return self._apply_operator(divmod, other, reverse=True)
    def __rfloordiv__(self, other):
        return self._apply_operator(operator.floordiv, other, reverse=True)
    def __rlshift__(self, other):
        return self._apply_operator(operator.rlshift, other)
    def __rmod__(self, other):
        return self._apply_operator(operator.mod, other, reverse=True)
    def __rmul__(self, other):
        return self._apply_operator(operator.mul, other, reverse=True)
    def __ror__(self, other):
        return self._apply_operator(operator.or_, other, reverse=True)
    def __rpow__(self, other):
        return self._apply_operator(operator.pow, other, reverse=True)
    def __rrshift__(self, other):
        return self._apply_operator(operator.rrshift, other)
    def __rsub__(self, other):
        return self._apply_operator(operator.sub, other, reverse=True)
    def __rtruediv__(self, other):
        return self._apply_operator(operator.truediv, other, reverse=True)
    def __rxor__(self, other):
        return self._apply_operator(operator.xor, other, reverse=True)

    def __getitem__(self, other):
        return self._apply_operator(operator.getitem, other)

    def _eval_operation(self, obj, operation):
        fn, args, kwargs = operation['fn'], operation['args'], operation['kwargs']
        resolved_args = []
        for arg in args:
            if isinstance(arg, interactive):
                arg = arg.eval()
                resolved_args.append(arg)
                continue

            arg = transform_dependency(arg)
            if hasattr(arg, '_dinfo'):
                arg = eval_function(arg)
            elif isinstance(arg, param.Parameter):
                arg = getattr(arg.owner, arg.name)
            resolved_args.append(arg)
        resolved_kwargs = {}
        for k, arg in kwargs.items():
            if isinstance(arg, interactive):
                arg = arg.eval()
                resolved_kwargs[k] = arg
                continue

            arg = transform_dependency(arg)
            if hasattr(arg, '_dinfo'):
                arg = eval_function(arg)
            elif isinstance(arg, param.Parameter):
                arg = getattr(arg.owner, arg.name)
            resolved_kwargs[k] = arg
        if isinstance(fn, str):
            obj = getattr(obj, fn)(*resolved_args, **resolved_kwargs)
        elif operation.get('reverse'):
            obj = fn(resolved_args[0], obj, *resolved_args[1:], **resolved_kwargs)
        else:
            print(fn, obj, resolved_args, resolved_kwargs)
            obj = fn(obj, *resolved_args, **resolved_kwargs)
        return obj

    #----------------------------------------------------------------
    # Public API
    #----------------------------------------------------------------

    def eval(self):
        """
        Returns the current state of the interactive expression. The
        returned object is no longer interactive.
        """
        if not self._dirty:
            return self._current_
        obj = self._obj if self._prev is None else self._prev.eval()
        operation = self._operation
        if operation:
            obj = self._eval_operation(obj, operation)
        self._current_ = obj
        self._dirty = False
        if self._method:
            # E.g. `pi = dfi.A` leads to `pi._method` equal to `'A'`.
            obj = getattr(obj, self._method, obj)
        if hasattr(obj, '__call__'):
            self.__call__.__func__.__doc__ = obj.__call__.__doc__
        return obj

    def set(self, new):
        """
        Allows overriding the original input to the pipeline.
        """
        prev = self
        while prev is not None:
            prev._dirty = True
            if prev._prev is None:
                if prev._wrapper is None:
                    raise ValueError(
                        'interactive.set is only supported if the root object '
                        'is a constant value. If the root is a Parameter or '
                        'another dynamic value it must reflect the source and '
                        'can not be set.'
                    )
                else:
                    prev._wrapper.object = new
            prev = prev._prev
        return self


class interactive(interactive_base):

    _display_opts = ('loc', 'center')

    def __init__(self, obj, **kwargs):
        display_opts = {}
        for dopt in self._display_opts:
            if dopt in kwargs:
                kwargs[dopt] = kwargs.pop(dopt)
        super().__init__(obj, **kwargs)
        self._display_opts = display_opts

    def _clone(self, operation=None, copy=False, **kwargs):
        kwargs.update(self._display_opts)
        return super()._clone(operation=operation, copy=copy, **kwargs)

    def _repr_mimebundle_(self, include=[], exclude=[]):
        return self.layout()._repr_mimebundle_()

    def __panel__(self):
        return self.layout()

    #----------------------------------------------------------------
    # Public API
    #----------------------------------------------------------------

    def layout(self, **kwargs):
        """
        Returns a layout of the widgets and output arranged according
        to the center and widget location specified in the
        interactive call.
        """
        widget_box = self.widgets()
        panel = self.output()
        loc = self._display_opts.get('loc', 'left')
        center = self._display_opts.get('center', False)
        alignments = {
            'left': (Row, ('start', 'center'), True),
            'right': (Row, ('end', 'center'), False),
            'top': (Column, ('center', 'start'), True),
            'bottom': (Column, ('center', 'end'), False),
            'top_left': (Column, 'start', True),
            'top_right': (Column, ('end', 'start'), True),
            'bottom_left': (Column, ('start', 'end'), False),
            'bottom_right': (Column, 'end', False),
            'left_top': (Row, 'start', True),
            'left_bottom': (Row, ('start', 'end'), True),
            'right_top': (Row, ('end', 'start'), False),
            'right_bottom': (Row, 'end', False)
        }
        layout, align, widget_first = alignments[loc]
        widget_box.align = align
        if not len(widget_box):
            if center:
                components = [HSpacer(), panel, HSpacer()]
            else:
                components = [panel]
            return Row(*components, **kwargs)

        items = (widget_box, panel) if widget_first else (panel, widget_box)
        sizing_mode = kwargs.get('sizing_mode')
        if not center:
            if layout is Row:
                components = list(items)
            else:
                components = [layout(*items, sizing_mode=sizing_mode)]
        elif layout is Column:
            components = [HSpacer(), layout(*items, sizing_mode=sizing_mode), HSpacer()]
        elif loc.startswith('left'):
            components = [widget_box, HSpacer(), panel, HSpacer()]
        else:
            components = [HSpacer(), panel, HSpacer(), widget_box]
        return Row(*components, **kwargs)

    def output(self):
        """
        Returns the output of the interactive pipeline, which is
        either a HoloViews DynamicMap or a Panel object.

        Returns
        -------
        DynamicMap or Panel object wrapping the interactive output.
        """
        return self.panel(**self._kwargs)

    def panel(self, **kwargs):
        """
        Wraps the output in a Panel component.
        """
        return panel(self._callback, **kwargs)

    def widgets(self):
        """
        Returns a Column of widgets which control the interactive output.

        Returns
        -------
        A Column of widgets
        """
        widgets = []
        for p in self._fn_params:
            if (isinstance(p.owner, Widget) and
                p.owner not in widgets):
                widgets.append(p.owner)
        prev = self
        while prev is not None:
            if prev._operation:
                for w in _find_widgets(prev._operation):
                    if w not in widgets:
                        widgets.append(w)
            prev = prev._prev
        return Column(*widgets)


def _interactive_transform(obj):
    if not isinstance(obj, interactive):
        return obj
    return bind(lambda *_: obj.eval(), *obj._params)

register_depends_transform(_interactive_transform)
