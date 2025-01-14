# Copyright 2016-2017 The Meson development team

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#     http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# This class contains the basic functionality needed to run any interpreter
# or an interpreter-based tool.

from . import mparser, mesonlib, mlog
from . import environment, dependencies

from functools import wraps
import abc
import collections.abc
import itertools
import os, copy, re
import typing as T

TV_fw_var = T.Union[str, int, float, bool, list, dict, 'InterpreterObject', 'ObjectHolder']
TV_fw_args = T.List[T.Union[mparser.BaseNode, TV_fw_var]]
TV_fw_kwargs = T.Dict[str, T.Union[mparser.BaseNode, TV_fw_var]]

TV_func = T.TypeVar('TV_func', bound=T.Callable[..., T.Any])

TYPE_elementary = T.Union[str, int, float, bool]
TYPE_var = T.Union[TYPE_elementary, T.List[T.Any], T.Dict[str, T.Any], 'InterpreterObject', 'ObjectHolder']
TYPE_nvar = T.Union[TYPE_var, mparser.BaseNode]
TYPE_nkwargs = T.Dict[str, TYPE_nvar]
TYPE_key_resolver = T.Callable[[mparser.BaseNode], str]

class InterpreterObject:
    def __init__(self) -> None:
        self.methods = {}  # type: T.Dict[str, T.Callable[[T.List[TYPE_nvar], TYPE_nkwargs], TYPE_var]]
        # Current node set during a method call. This can be used as location
        # when printing a warning message during a method call.
        self.current_node = None  # type: mparser.BaseNode

    def method_call(
                self,
                method_name: str,
                args: TV_fw_args,
                kwargs: TV_fw_kwargs
            ) -> TYPE_var:
        if method_name in self.methods:
            method = self.methods[method_name]
            if not getattr(method, 'no-args-flattening', False):
                args = flatten(args)
            return method(args, kwargs)
        raise InvalidCode('Unknown method "%s" in object.' % method_name)

TV_InterpreterObject = T.TypeVar('TV_InterpreterObject')

class ObjectHolder(T.Generic[TV_InterpreterObject]):
    def __init__(self, obj: TV_InterpreterObject, subproject: str = '') -> None:
        self.held_object = obj
        self.subproject = subproject

    def __repr__(self) -> str:
        return f'<Holder: {self.held_object!r}>'

class MesonVersionString(str):
    pass

class RangeHolder(InterpreterObject):
    def __init__(self, start: int, stop: int, step: int) -> None:
        super().__init__()
        self.range = range(start, stop, step)

    def __iter__(self) -> T.Iterator[int]:
        return iter(self.range)

    def __getitem__(self, key: int) -> int:
        return self.range[key]

    def __len__(self) -> int:
        return len(self.range)

# Decorators for method calls.

def check_stringlist(a: T.Any, msg: str = 'Arguments must be strings.') -> None:
    if not isinstance(a, list):
        mlog.debug('Not a list:', str(a))
        raise InvalidArguments('Argument not a list.')
    if not all(isinstance(s, str) for s in a):
        mlog.debug('Element not a string:', str(a))
        raise InvalidArguments(msg)

def _get_callee_args(wrapped_args: T.Sequence[T.Any], want_subproject: bool = False) -> T.Tuple[T.Any, mparser.BaseNode, TV_fw_args, TV_fw_kwargs, T.Optional[str]]:
    s = wrapped_args[0]
    n = len(wrapped_args)
    # Raise an error if the codepaths are not there
    subproject = None  # type: T.Optional[str]
    if want_subproject and n == 2:
        if hasattr(s, 'subproject'):
            # Interpreter base types have 2 args: self, node
            node = wrapped_args[1]
            # args and kwargs are inside the node
            args = None
            kwargs = None
            subproject = s.subproject
        elif hasattr(wrapped_args[1], 'subproject'):
            # Module objects have 2 args: self, interpreter
            node = wrapped_args[1].current_node
            # args and kwargs are inside the node
            args = None
            kwargs = None
            subproject = wrapped_args[1].subproject
        else:
            raise AssertionError(f'Unknown args: {wrapped_args!r}')
    elif n == 3:
        # Methods on objects (*Holder, MesonMain, etc) have 3 args: self, args, kwargs
        node = s.current_node
        args = wrapped_args[1]
        kwargs = wrapped_args[2]
        if want_subproject:
            if hasattr(s, 'subproject'):
                subproject = s.subproject
            elif hasattr(s, 'interpreter'):
                subproject = s.interpreter.subproject
    elif n == 4:
        # Meson functions have 4 args: self, node, args, kwargs
        # Module functions have 4 args: self, state, args, kwargs
        if isinstance(s, InterpreterBase):
            node = wrapped_args[1]
        else:
            node = wrapped_args[1].current_node
        args = wrapped_args[2]
        kwargs = wrapped_args[3]
        if want_subproject:
            if isinstance(s, InterpreterBase):
                subproject = s.subproject
            else:
                subproject = wrapped_args[1].subproject
    else:
        raise AssertionError(f'Unknown args: {wrapped_args!r}')
    # Sometimes interpreter methods are called internally with None instead of
    # empty list/dict
    args = args if args is not None else []
    kwargs = kwargs if kwargs is not None else {}
    return s, node, args, kwargs, subproject

def flatten(args: T.Union[TYPE_nvar, T.List[TYPE_nvar]]) -> T.List[TYPE_nvar]:
    if isinstance(args, mparser.StringNode):
        assert isinstance(args.value, str)
        return [args.value]
    if not isinstance(args, collections.abc.Sequence):
        return [args]
    result = []  # type: T.List[TYPE_nvar]
    for a in args:
        if isinstance(a, list):
            rest = flatten(a)
            result = result + rest
        elif isinstance(a, mparser.StringNode):
            result.append(a.value)
        else:
            result.append(a)
    return result

def noPosargs(f: TV_func) -> TV_func:
    @wraps(f)
    def wrapped(*wrapped_args: T.Any, **wrapped_kwargs: T.Any) -> T.Any:
        args = _get_callee_args(wrapped_args)[2]
        if args:
            raise InvalidArguments('Function does not take positional arguments.')
        return f(*wrapped_args, **wrapped_kwargs)
    return T.cast(TV_func, wrapped)

def builtinMethodNoKwargs(f: TV_func) -> TV_func:
    @wraps(f)
    def wrapped(*wrapped_args: T.Any, **wrapped_kwargs: T.Any) -> T.Any:
        node = wrapped_args[0].current_node
        method_name = wrapped_args[2]
        kwargs = wrapped_args[4]
        if kwargs:
            mlog.warning(f'Method {method_name!r} does not take keyword arguments.',
                         'This will become a hard error in the future',
                         location=node)
        return f(*wrapped_args, **wrapped_kwargs)
    return T.cast(TV_func, wrapped)

def noKwargs(f: TV_func) -> TV_func:
    @wraps(f)
    def wrapped(*wrapped_args: T.Any, **wrapped_kwargs: T.Any) -> T.Any:
        kwargs = _get_callee_args(wrapped_args)[3]
        if kwargs:
            raise InvalidArguments('Function does not take keyword arguments.')
        return f(*wrapped_args, **wrapped_kwargs)
    return T.cast(TV_func, wrapped)

def stringArgs(f: TV_func) -> TV_func:
    @wraps(f)
    def wrapped(*wrapped_args: T.Any, **wrapped_kwargs: T.Any) -> T.Any:
        args = _get_callee_args(wrapped_args)[2]
        assert(isinstance(args, list))
        check_stringlist(args)
        return f(*wrapped_args, **wrapped_kwargs)
    return T.cast(TV_func, wrapped)

def noArgsFlattening(f: TV_func) -> TV_func:
    setattr(f, 'no-args-flattening', True)  # noqa: B010
    return f

def disablerIfNotFound(f: TV_func) -> TV_func:
    @wraps(f)
    def wrapped(*wrapped_args: T.Any, **wrapped_kwargs: T.Any) -> T.Any:
        kwargs = _get_callee_args(wrapped_args)[3]
        disabler = kwargs.pop('disabler', False)
        ret = f(*wrapped_args, **wrapped_kwargs)
        if disabler and not ret.held_object.found():
            return Disabler()
        return ret
    return T.cast(TV_func, wrapped)

class permittedKwargs:

    def __init__(self, permitted: T.Set[str]):
        self.permitted = permitted  # type: T.Set[str]

    def __call__(self, f: TV_func) -> TV_func:
        @wraps(f)
        def wrapped(*wrapped_args: T.Any, **wrapped_kwargs: T.Any) -> T.Any:
            s, node, args, kwargs, _ = _get_callee_args(wrapped_args)
            for k in kwargs:
                if k not in self.permitted:
                    mlog.warning(f'''Passed invalid keyword argument "{k}".''', location=node)
                    mlog.warning('This will become a hard error in the future.')
            return f(*wrapped_args, **wrapped_kwargs)
        return T.cast(TV_func, wrapped)


def typed_pos_args(name: str, *types: T.Union[T.Type, T.Tuple[T.Type, ...]],
                   varargs: T.Optional[T.Union[T.Type, T.Tuple[T.Type]]] = None,
                   optargs: T.Optional[T.List[T.Union[T.Type, T.Tuple[T.Type]]]] = None,
                   min_varargs: int = 0, max_varargs: int = 0) -> T.Callable[..., T.Any]:
    """Decorator that types type checking of positional arguments.

    This supports two different models of optional aguments, the first is the
    variadic argument model. Variadic arguments are a possibly bounded,
    possibly unbounded number of arguments of the same type (unions are
    supported). The second is the standard default value model, in this case
    a number of optional arguments may be provided, but they are still
    ordered, and they may have different types.

    This function does not support mixing variadic and default arguments.

    :name: The name of the decorated function (as displayed in error messages)
    :varargs: They type(s) of any variadic arguments the function takes. If
        None the function takes no variadic args
    :min_varargs: the minimum number of variadic arguments taken
    :max_varargs: the maximum number of variadic arguments taken. 0 means unlimited
    :optargs: The types of any optional arguments parameters taken. If None
        then no optional paramters are taken.

    Some examples of usage blow:
    >>> @typed_pos_args('mod.func', str, (str, int))
    ... def func(self, state: ModuleState, args: T.Tuple[str, T.Union[str, int]], kwargs: T.Dict[str, T.Any]) -> T.Any:
    ...     pass

    >>> @typed_pos_args('method', str, varargs=str)
    ... def method(self, node: BaseNode, args: T.Tuple[str, T.List[str]], kwargs: T.Dict[str, T.Any]) -> T.Any:
    ...     pass

    >>> @typed_pos_args('method', varargs=str, min_varargs=1)
    ... def method(self, node: BaseNode, args: T.Tuple[T.List[str]], kwargs: T.Dict[str, T.Any]) -> T.Any:
    ...     pass

    >>> @typed_pos_args('method', str, optargs=[(str, int), str])
    ... def method(self, node: BaseNode, args: T.Tuple[str, T.Optional[T.Union[str, int]], T.Optional[str]], kwargs: T.Dict[str, T.Any]) -> T.Any:
    ...     pass

    When should you chose `typed_pos_args('name', varargs=str,
    min_varargs=1)` vs `typed_pos_args('name', str, varargs=str)`?

    The answer has to do with the semantics of the function, if all of the
    inputs are the same type (such as with `files()`) then the former is
    correct, all of the arguments are string names of files. If the first
    argument is something else the it should be separated.
    """
    def inner(f: TV_func) -> TV_func:

        @wraps(f)
        def wrapper(*wrapped_args: T.Any, **wrapped_kwargs: T.Any) -> T.Any:
            args = _get_callee_args(wrapped_args)[2]

            # These are implementation programming errors, end users should never see them.
            assert isinstance(args, list), args
            assert max_varargs >= 0, 'max_varags cannot be negative'
            assert min_varargs >= 0, 'min_varags cannot be negative'
            assert optargs is None or varargs is None, \
                'varargs and optargs not supported together as this would be ambiguous'

            num_args = len(args)
            num_types = len(types)
            a_types = types

            if varargs:
                min_args = num_types + min_varargs
                max_args = num_types + max_varargs
                if max_varargs == 0 and num_args < min_args:
                    raise InvalidArguments(f'{name} takes at least {min_args} arguments, but got {num_args}.')
                elif max_varargs != 0 and (num_args < min_args or num_args > max_args):
                    raise InvalidArguments(f'{name} takes between {min_args} and {max_args} arguments, but got {num_args}.')
            elif optargs:
                if num_args < num_types:
                    raise InvalidArguments(f'{name} takes at least {num_types} arguments, but got {num_args}.')
                elif num_args > num_types + len(optargs):
                    raise InvalidArguments(f'{name} takes at most {num_types + len(optargs)} arguments, but got {num_args}.')
                # Add the number of positional arguments required
                if num_args > num_types:
                    diff = num_args - num_types
                    a_types = tuple(list(types) + list(optargs[:diff]))
            elif num_args != num_types:
                raise InvalidArguments(f'{name} takes exactly {num_types} arguments, but got {num_args}.')

            for i, (arg, type_) in enumerate(itertools.zip_longest(args, a_types, fillvalue=varargs), start=1):
                if not isinstance(arg, type_):
                    if isinstance(type_, tuple):
                        shouldbe = 'one of: {}'.format(", ".join(f'"{t.__name__}"' for t in type_))
                    else:
                        shouldbe = f'"{type_.__name__}"'
                    raise InvalidArguments(f'{name} argument {i} was of type "{type(arg).__name__}" but should have been {shouldbe}')

            # Ensure that we're actually passing a tuple.
            # Depending on what kind of function we're calling the length of
            # wrapped_args can vary.
            nargs = list(wrapped_args)
            i = nargs.index(args)
            if varargs:
                # if we have varargs we need to split them into a separate
                # tuple, as python's typing doesn't understand tuples with
                # fixed elements and variadic elements, only one or the other.
                # so in that case we need T.Tuple[int, str, float, T.Tuple[str, ...]]
                pos = args[:len(types)]
                var = list(args[len(types):])
                pos.append(var)
                nargs[i] = tuple(pos)
            elif optargs:
                if num_args < num_types + len(optargs):
                    diff =  num_types + len(optargs) - num_args
                    nargs[i] = tuple(list(args) + [None] * diff)
                else:
                    nargs[i] = args
            else:
                nargs[i] = tuple(args)
            return f(*nargs, **wrapped_kwargs)

        return T.cast(TV_func, wrapper)
    return inner


class ContainerTypeInfo:

    """Container information for keyword arguments.

    For keyword arguments that are containers (list or dict), this class encodes
    that information.

    :param container: the type of container
    :param contains: the types the container holds
    :param pairs: if the container is supposed to be of even length.
        This is mainly used for interfaces that predate the addition of dictionaries, and use
        `[key, value, key2, value2]` format.
    :param allow_empty: Whether this container is allowed to be empty
        There are some cases where containers not only must be passed, but must
        not be empty, and other cases where an empty container is allowed.
    """

    def __init__(self, container: T.Type, contains: T.Union[T.Type, T.Tuple[T.Type, ...]], *,
                 pairs: bool = False, allow_empty: bool = True) :
        self.container = container
        self.contains = contains
        self.pairs = pairs
        self.allow_empty = allow_empty

    def check(self, value: T.Any) -> T.Optional[str]:
        """Check that a value is valid.

        :param value: A value to check
        :return: If there is an error then a string message, otherwise None
        """
        if not isinstance(value, self.container):
            return f'container type was "{type(value).__name__}", but should have been "{self.container.__name__}"'
        iter_ = iter(value.values()) if isinstance(value, dict) else iter(value)
        for each in iter_:
            if not isinstance(each, self.contains):
                if isinstance(self.contains, tuple):
                    shouldbe = 'one of: {}'.format(", ".join(f'"{t.__name__}"' for t in self.contains))
                else:
                    shouldbe = f'"{self.contains.__name__}"'
                return f'contained a value of type "{type(each).__name__}" but should have been {shouldbe}'
        if self.pairs and len(value) % 2 != 0:
            return 'container should be of even length, but is not'
        if not value and not self.allow_empty:
            return 'container is empty, but not allowed to be'
        return None


_T = T.TypeVar('_T')


class KwargInfo(T.Generic[_T]):

    """A description of a keyword argument to a meson function

    This is used to describe a value to the :func:typed_kwargs function.

    :param name: the name of the parameter
    :param types: A type or tuple of types that are allowed, or a :class:ContainerType
    :param required: Whether this is a required keyword argument. defaults to False
    :param listify: If true, then the argument will be listified before being
        checked. This is useful for cases where the Meson DSL allows a scalar or
        a container, but internally we only want to work with containers
    :param default: A default value to use if this isn't set. defaults to None
    :param since: Meson version in which this argument has been added. defaults to None
    :param deprecated: Meson version in which this argument has been deprecated. defaults to None
    """

    def __init__(self, name: str, types: T.Union[T.Type[_T], T.Tuple[T.Type[_T], ...], ContainerTypeInfo],
                 required: bool = False, listify: bool = False, default: T.Optional[_T] = None,
                 since: T.Optional[str] = None, deprecated: T.Optional[str] = None):
        self.name = name
        self.types = types
        self.required = required
        self.listify = listify
        self.default = default
        self.since = since
        self.deprecated = deprecated


def typed_kwargs(name: str, *types: KwargInfo) -> T.Callable[..., T.Any]:
    """Decorator for type checking keyword arguments.

    Used to wrap a meson DSL implementation function, where it checks various
    things about keyword arguments, including the type, and various other
    information. For non-required values it sets the value to a default, which
    means the value will always be provided.

    :param name: the name of the function, including the object it's attached ot
        (if applicable)
    :param *types: KwargInfo entries for each keyword argument.
    """
    def inner(f: TV_func) -> TV_func:

        @wraps(f)
        def wrapper(*wrapped_args: T.Any, **wrapped_kwargs: T.Any) -> T.Any:
            kwargs, subproject = _get_callee_args(wrapped_args, want_subproject=True)[3:5]

            all_names = {t.name for t in types}
            unknowns = set(kwargs).difference(all_names)
            if unknowns:
                # Warn about unknown argumnts, delete them and continue. This
                # keeps current behavior
                ustr = ', '.join([f'"{u}"' for u in sorted(unknowns)])
                mlog.warning(f'{name} got unknown keyword arguments {ustr}')
                for u in unknowns:
                    del kwargs[u]

            for info in types:
                value = kwargs.get(info.name)
                if value is not None:
                    if info.since:
                        feature_name = info.name + ' arg in ' + name
                        FeatureNew.single_use(feature_name, info.since, subproject)
                    if info.deprecated:
                        feature_name = info.name + ' arg in ' + name
                        FeatureDeprecated.single_use(feature_name, info.deprecated, subproject)
                    if info.listify:
                        kwargs[info.name] = value = mesonlib.listify(value)
                    if isinstance(info.types, ContainerTypeInfo):
                        msg = info.types.check(value)
                        if msg is not None:
                            raise InvalidArguments(f'{name} keyword argument "{info.name}" {msg}')
                    else:
                        if not isinstance(value, info.types):
                            if isinstance(info.types, tuple):
                                shouldbe = 'one of: {}'.format(", ".join(f'"{t.__name__}"' for t in info.types))
                            else:
                                shouldbe = f'"{info.types.__name__}"'
                            raise InvalidArguments(f'{name} keyword argument "{info.name}"" was of type "{type(value).__name__}" but should have been {shouldbe}')
                elif info.required:
                    raise InvalidArguments(f'{name} is missing required keyword argument "{info.name}"')
                else:
                    # set the value to the default, this ensuring all kwargs are present
                    # This both simplifies the typing checking and the usage
                    kwargs[info.name] = info.default

            return f(*wrapped_args, **wrapped_kwargs)
        return T.cast(TV_func, wrapper)
    return inner


class FeatureCheckBase(metaclass=abc.ABCMeta):
    "Base class for feature version checks"

    # In python 3.6 we can just forward declare this, but in 3.5 we can't
    # This will be overwritten by the subclasses by necessity
    feature_registry = {}  # type: T.ClassVar[T.Dict[str, T.Dict[str, T.Set[str]]]]

    def __init__(self, feature_name: str, version: str, extra_message: T.Optional[str] = None):
        self.feature_name = feature_name  # type: str
        self.feature_version = version    # type: str
        self.extra_message = extra_message or ''  # type: str

    @staticmethod
    def get_target_version(subproject: str) -> str:
        # Don't do any checks if project() has not been parsed yet
        if subproject not in mesonlib.project_meson_versions:
            return ''
        return mesonlib.project_meson_versions[subproject]

    @staticmethod
    @abc.abstractmethod
    def check_version(target_version: str, feature_Version: str) -> bool:
        pass

    def use(self, subproject: str) -> None:
        tv = self.get_target_version(subproject)
        # No target version
        if tv == '':
            return
        # Target version is new enough
        if self.check_version(tv, self.feature_version):
            return
        # Feature is too new for target version, register it
        if subproject not in self.feature_registry:
            self.feature_registry[subproject] = {self.feature_version: set()}
        register = self.feature_registry[subproject]
        if self.feature_version not in register:
            register[self.feature_version] = set()
        if self.feature_name in register[self.feature_version]:
            # Don't warn about the same feature multiple times
            # FIXME: This is needed to prevent duplicate warnings, but also
            # means we won't warn about a feature used in multiple places.
            return
        register[self.feature_version].add(self.feature_name)
        self.log_usage_warning(tv)

    @classmethod
    def report(cls, subproject: str) -> None:
        if subproject not in cls.feature_registry:
            return
        warning_str = cls.get_warning_str_prefix(cls.get_target_version(subproject))
        fv = cls.feature_registry[subproject]
        for version in sorted(fv.keys()):
            warning_str += '\n * {}: {}'.format(version, fv[version])
        mlog.warning(warning_str)

    def log_usage_warning(self, tv: str) -> None:
        raise InterpreterException('log_usage_warning not implemented')

    @staticmethod
    def get_warning_str_prefix(tv: str) -> str:
        raise InterpreterException('get_warning_str_prefix not implemented')

    def __call__(self, f: TV_func) -> TV_func:
        @wraps(f)
        def wrapped(*wrapped_args: T.Any, **wrapped_kwargs: T.Any) -> T.Any:
            subproject = _get_callee_args(wrapped_args, want_subproject=True)[4]
            if subproject is None:
                raise AssertionError(f'{wrapped_args!r}')
            self.use(subproject)
            return f(*wrapped_args, **wrapped_kwargs)
        return T.cast(TV_func, wrapped)

    @classmethod
    def single_use(cls, feature_name: str, version: str, subproject: str,
                   extra_message: T.Optional[str] = None) -> None:
        """Oneline version that instantiates and calls use()."""
        cls(feature_name, version, extra_message).use(subproject)


class FeatureNew(FeatureCheckBase):
    """Checks for new features"""

    # Class variable, shared across all instances
    #
    # Format: {subproject: {feature_version: set(feature_names)}}
    feature_registry = {}  # type: T.ClassVar[T.Dict[str, T.Dict[str, T.Set[str]]]]

    @staticmethod
    def check_version(target_version: str, feature_version: str) -> bool:
        return mesonlib.version_compare_condition_with_min(target_version, feature_version)

    @staticmethod
    def get_warning_str_prefix(tv: str) -> str:
        return f'Project specifies a minimum meson_version \'{tv}\' but uses features which were added in newer versions:'

    def log_usage_warning(self, tv: str) -> None:
        args = [
            'Project targeting', f"'{tv}'",
            'but tried to use feature introduced in',
            f"'{self.feature_version}':",
            f'{self.feature_name}.',
        ]
        if self.extra_message:
            args.append(self.extra_message)
        mlog.warning(*args)

class FeatureDeprecated(FeatureCheckBase):
    """Checks for deprecated features"""

    # Class variable, shared across all instances
    #
    # Format: {subproject: {feature_version: set(feature_names)}}
    feature_registry = {}  # type: T.ClassVar[T.Dict[str, T.Dict[str, T.Set[str]]]]

    @staticmethod
    def check_version(target_version: str, feature_version: str) -> bool:
        # For deprecation checks we need to return the inverse of FeatureNew checks
        return not mesonlib.version_compare_condition_with_min(target_version, feature_version)

    @staticmethod
    def get_warning_str_prefix(tv: str) -> str:
        return 'Deprecated features used:'

    def log_usage_warning(self, tv: str) -> None:
        args = [
            'Project targeting', f"'{tv}'",
            'but tried to use feature deprecated since',
            f"'{self.feature_version}':",
            f'{self.feature_name}.',
        ]
        if self.extra_message:
            args.append(self.extra_message)
        mlog.warning(*args)


class FeatureCheckKwargsBase(metaclass=abc.ABCMeta):

    @property
    @abc.abstractmethod
    def feature_check_class(self) -> T.Type[FeatureCheckBase]:
        pass

    def __init__(self, feature_name: str, feature_version: str,
                 kwargs: T.List[str], extra_message: T.Optional[str] = None):
        self.feature_name = feature_name
        self.feature_version = feature_version
        self.kwargs = kwargs
        self.extra_message = extra_message

    def __call__(self, f: TV_func) -> TV_func:
        @wraps(f)
        def wrapped(*wrapped_args: T.Any, **wrapped_kwargs: T.Any) -> T.Any:
            kwargs, subproject = _get_callee_args(wrapped_args, want_subproject=True)[3:5]
            if subproject is None:
                raise AssertionError(f'{wrapped_args!r}')
            for arg in self.kwargs:
                if arg not in kwargs:
                    continue
                name = arg + ' arg in ' + self.feature_name
                self.feature_check_class.single_use(
                        name, self.feature_version, subproject, self.extra_message)
            return f(*wrapped_args, **wrapped_kwargs)
        return T.cast(TV_func, wrapped)

class FeatureNewKwargs(FeatureCheckKwargsBase):
    feature_check_class = FeatureNew

class FeatureDeprecatedKwargs(FeatureCheckKwargsBase):
    feature_check_class = FeatureDeprecated


class InterpreterException(mesonlib.MesonException):
    pass

class InvalidCode(InterpreterException):
    pass

class InvalidArguments(InterpreterException):
    pass

class SubdirDoneRequest(BaseException):
    pass

class ContinueRequest(BaseException):
    pass

class BreakRequest(BaseException):
    pass

class MutableInterpreterObject(InterpreterObject):
    def __init__(self) -> None:
        super().__init__()

class Disabler(InterpreterObject):
    def __init__(self) -> None:
        super().__init__()
        self.methods.update({'found': self.found_method})

    def found_method(self, args: T.Sequence[T.Any], kwargs: T.Dict[str, T.Any]) -> bool:
        return False

def is_disabler(i: T.Any) -> bool:
    return isinstance(i, Disabler)

def is_arg_disabled(arg: T.Any) -> bool:
    if is_disabler(arg):
        return True
    if isinstance(arg, list):
        for i in arg:
            if is_arg_disabled(i):
                return True
    return False

def is_disabled(args: T.Sequence[T.Any], kwargs: T.Dict[str, T.Any]) -> bool:
    for i in args:
        if is_arg_disabled(i):
            return True
    for i in kwargs.values():
        if is_arg_disabled(i):
            return True
    return False

def default_resolve_key(key: mparser.BaseNode) -> str:
    if not isinstance(key, mparser.IdNode):
        raise InterpreterException('Invalid kwargs format.')
    return key.value

class InterpreterBase:
    elementary_types = (int, float, str, bool, list)

    def __init__(self, source_root: str, subdir: str, subproject: str):
        self.source_root = source_root
        self.funcs = {}    # type: T.Dict[str, T.Callable[[mparser.BaseNode, T.List[TYPE_nvar], T.Dict[str, TYPE_nvar]], TYPE_var]]
        self.builtin = {}  # type: T.Dict[str, InterpreterObject]
        self.subdir = subdir
        self.root_subdir = subdir
        self.subproject = subproject
        self.variables = {}  # type: T.Dict[str, TYPE_var]
        self.argument_depth = 0
        self.current_lineno = -1
        # Current node set during a function call. This can be used as location
        # when printing a warning message during a method call.
        self.current_node = None  # type: mparser.BaseNode
        # This is set to `version_string` when this statement is evaluated:
        # meson.version().compare_version(version_string)
        # If it was part of a if-clause, it is used to temporally override the
        # current meson version target within that if-block.
        self.tmp_meson_version = None # type: T.Optional[str]

    def load_root_meson_file(self) -> None:
        mesonfile = os.path.join(self.source_root, self.subdir, environment.build_filename)
        if not os.path.isfile(mesonfile):
            raise InvalidArguments('Missing Meson file in %s' % mesonfile)
        with open(mesonfile, encoding='utf8') as mf:
            code = mf.read()
        if code.isspace():
            raise InvalidCode('Builder file is empty.')
        assert(isinstance(code, str))
        try:
            self.ast = mparser.Parser(code, mesonfile).parse()
        except mesonlib.MesonException as me:
            me.file = mesonfile
            raise me

    def join_path_strings(self, args: T.Sequence[str]) -> str:
        return os.path.join(*args).replace('\\', '/')

    def parse_project(self) -> None:
        """
        Parses project() and initializes languages, compilers etc. Do this
        early because we need this before we parse the rest of the AST.
        """
        self.evaluate_codeblock(self.ast, end=1)

    def sanity_check_ast(self) -> None:
        if not isinstance(self.ast, mparser.CodeBlockNode):
            raise InvalidCode('AST is of invalid type. Possibly a bug in the parser.')
        if not self.ast.lines:
            raise InvalidCode('No statements in code.')
        first = self.ast.lines[0]
        if not isinstance(first, mparser.FunctionNode) or first.func_name != 'project':
            raise InvalidCode('First statement must be a call to project')

    def run(self) -> None:
        # Evaluate everything after the first line, which is project() because
        # we already parsed that in self.parse_project()
        try:
            self.evaluate_codeblock(self.ast, start=1)
        except SubdirDoneRequest:
            pass

    def evaluate_codeblock(self, node: mparser.CodeBlockNode, start: int = 0, end: T.Optional[int] = None) -> None:
        if node is None:
            return
        if not isinstance(node, mparser.CodeBlockNode):
            e = InvalidCode('Tried to execute a non-codeblock. Possibly a bug in the parser.')
            e.lineno = node.lineno
            e.colno = node.colno
            raise e
        statements = node.lines[start:end]
        i = 0
        while i < len(statements):
            cur = statements[i]
            try:
                self.current_lineno = cur.lineno
                self.evaluate_statement(cur)
            except Exception as e:
                if getattr(e, 'lineno', None) is None:
                    # We are doing the equivalent to setattr here and mypy does not like it
                    e.lineno = cur.lineno                                                             # type: ignore
                    e.colno = cur.colno                                                               # type: ignore
                    e.file = os.path.join(self.source_root, self.subdir, environment.build_filename)  # type: ignore
                raise e
            i += 1 # In THE FUTURE jump over blocks and stuff.

    def evaluate_statement(self, cur: mparser.BaseNode) -> T.Optional[TYPE_var]:
        self.current_node = cur
        if isinstance(cur, mparser.FunctionNode):
            return self.function_call(cur)
        elif isinstance(cur, mparser.AssignmentNode):
            self.assignment(cur)
        elif isinstance(cur, mparser.MethodNode):
            return self.method_call(cur)
        elif isinstance(cur, mparser.StringNode):
            return cur.value
        elif isinstance(cur, mparser.BooleanNode):
            return cur.value
        elif isinstance(cur, mparser.IfClauseNode):
            return self.evaluate_if(cur)
        elif isinstance(cur, mparser.IdNode):
            return self.get_variable(cur.value)
        elif isinstance(cur, mparser.ComparisonNode):
            return self.evaluate_comparison(cur)
        elif isinstance(cur, mparser.ArrayNode):
            return self.evaluate_arraystatement(cur)
        elif isinstance(cur, mparser.DictNode):
            return self.evaluate_dictstatement(cur)
        elif isinstance(cur, mparser.NumberNode):
            return cur.value
        elif isinstance(cur, mparser.AndNode):
            return self.evaluate_andstatement(cur)
        elif isinstance(cur, mparser.OrNode):
            return self.evaluate_orstatement(cur)
        elif isinstance(cur, mparser.NotNode):
            return self.evaluate_notstatement(cur)
        elif isinstance(cur, mparser.UMinusNode):
            return self.evaluate_uminusstatement(cur)
        elif isinstance(cur, mparser.ArithmeticNode):
            return self.evaluate_arithmeticstatement(cur)
        elif isinstance(cur, mparser.ForeachClauseNode):
            self.evaluate_foreach(cur)
        elif isinstance(cur, mparser.PlusAssignmentNode):
            self.evaluate_plusassign(cur)
        elif isinstance(cur, mparser.IndexNode):
            return self.evaluate_indexing(cur)
        elif isinstance(cur, mparser.TernaryNode):
            return self.evaluate_ternary(cur)
        elif isinstance(cur, mparser.FormatStringNode):
            return self.evaluate_fstring(cur)
        elif isinstance(cur, mparser.ContinueNode):
            raise ContinueRequest()
        elif isinstance(cur, mparser.BreakNode):
            raise BreakRequest()
        elif isinstance(cur, self.elementary_types):
            return cur
        else:
            raise InvalidCode("Unknown statement.")
        return None

    def evaluate_arraystatement(self, cur: mparser.ArrayNode) -> list:
        (arguments, kwargs) = self.reduce_arguments(cur.args)
        if len(kwargs) > 0:
            raise InvalidCode('Keyword arguments are invalid in array construction.')
        return arguments

    @FeatureNew('dict', '0.47.0')
    def evaluate_dictstatement(self, cur: mparser.DictNode) -> TYPE_nkwargs:
        def resolve_key(key: mparser.BaseNode) -> str:
            if not isinstance(key, mparser.StringNode):
                FeatureNew.single_use('Dictionary entry using non literal key', '0.53.0', self.subproject)
            str_key = self.evaluate_statement(key)
            if not isinstance(str_key, str):
                raise InvalidArguments('Key must be a string')
            return str_key
        arguments, kwargs = self.reduce_arguments(cur.args, key_resolver=resolve_key, duplicate_key_error='Duplicate dictionary key: {}')
        assert not arguments
        return kwargs

    def evaluate_notstatement(self, cur: mparser.NotNode) -> T.Union[bool, Disabler]:
        v = self.evaluate_statement(cur.value)
        if isinstance(v, Disabler):
            return v
        if not isinstance(v, bool):
            raise InterpreterException('Argument to "not" is not a boolean.')
        return not v

    def evaluate_if(self, node: mparser.IfClauseNode) -> T.Optional[Disabler]:
        assert(isinstance(node, mparser.IfClauseNode))
        for i in node.ifs:
            # Reset self.tmp_meson_version to know if it gets set during this
            # statement evaluation.
            self.tmp_meson_version = None
            result = self.evaluate_statement(i.condition)
            if isinstance(result, Disabler):
                return result
            if not(isinstance(result, bool)):
                raise InvalidCode(f'If clause {result!r} does not evaluate to true or false.')
            if result:
                prev_meson_version = mesonlib.project_meson_versions[self.subproject]
                if self.tmp_meson_version:
                    mesonlib.project_meson_versions[self.subproject] = self.tmp_meson_version
                try:
                    self.evaluate_codeblock(i.block)
                finally:
                    mesonlib.project_meson_versions[self.subproject] = prev_meson_version
                return None
        if not isinstance(node.elseblock, mparser.EmptyNode):
            self.evaluate_codeblock(node.elseblock)
        return None

    def validate_comparison_types(self, val1: T.Any, val2: T.Any) -> bool:
        if type(val1) != type(val2):
            return False
        return True

    def evaluate_in(self, val1: T.Any, val2: T.Any) -> bool:
        if not isinstance(val1, (str, int, float, ObjectHolder)):
            raise InvalidArguments('lvalue of "in" operator must be a string, integer, float, or object')
        if not isinstance(val2, (list, dict)):
            raise InvalidArguments('rvalue of "in" operator must be an array or a dict')
        return val1 in val2

    def evaluate_comparison(self, node: mparser.ComparisonNode) -> T.Union[bool, Disabler]:
        val1 = self.evaluate_statement(node.left)
        if isinstance(val1, Disabler):
            return val1
        val2 = self.evaluate_statement(node.right)
        if isinstance(val2, Disabler):
            return val2
        if node.ctype == 'in':
            return self.evaluate_in(val1, val2)
        elif node.ctype == 'notin':
            return not self.evaluate_in(val1, val2)
        valid = self.validate_comparison_types(val1, val2)
        # Ordering comparisons of different types isn't allowed since PR #1810
        # (0.41.0).  Since PR #2884 we also warn about equality comparisons of
        # different types, which will one day become an error.
        if not valid and (node.ctype == '==' or node.ctype == '!='):
            mlog.warning('''Trying to compare values of different types ({}, {}) using {}.
The result of this is undefined and will become a hard error in a future Meson release.'''
                         .format(type(val1).__name__, type(val2).__name__, node.ctype), location=node)
        if node.ctype == '==':
            return val1 == val2
        elif node.ctype == '!=':
            return val1 != val2
        elif not valid:
            raise InterpreterException(
                'Values of different types ({}, {}) cannot be compared using {}.'.format(type(val1).__name__,
                                                                                         type(val2).__name__,
                                                                                         node.ctype))
        elif not isinstance(val1, self.elementary_types):
            raise InterpreterException('{} can only be compared for equality.'.format(getattr(node.left, 'value', '<ERROR>')))
        elif not isinstance(val2, self.elementary_types):
            raise InterpreterException('{} can only be compared for equality.'.format(getattr(node.right, 'value', '<ERROR>')))
        # Use type: ignore because mypy will complain that we are comparing two Unions,
        # but we actually guarantee earlier that both types are the same
        elif node.ctype == '<':
            return val1 < val2   # type: ignore
        elif node.ctype == '<=':
            return val1 <= val2  # type: ignore
        elif node.ctype == '>':
            return val1 > val2   # type: ignore
        elif node.ctype == '>=':
            return val1 >= val2  # type: ignore
        else:
            raise InvalidCode('You broke my compare eval.')

    def evaluate_andstatement(self, cur: mparser.AndNode) -> T.Union[bool, Disabler]:
        l = self.evaluate_statement(cur.left)
        if isinstance(l, Disabler):
            return l
        if not isinstance(l, bool):
            raise InterpreterException('First argument to "and" is not a boolean.')
        if not l:
            return False
        r = self.evaluate_statement(cur.right)
        if isinstance(r, Disabler):
            return r
        if not isinstance(r, bool):
            raise InterpreterException('Second argument to "and" is not a boolean.')
        return r

    def evaluate_orstatement(self, cur: mparser.OrNode) -> T.Union[bool, Disabler]:
        l = self.evaluate_statement(cur.left)
        if isinstance(l, Disabler):
            return l
        if not isinstance(l, bool):
            raise InterpreterException('First argument to "or" is not a boolean.')
        if l:
            return True
        r = self.evaluate_statement(cur.right)
        if isinstance(r, Disabler):
            return r
        if not isinstance(r, bool):
            raise InterpreterException('Second argument to "or" is not a boolean.')
        return r

    def evaluate_uminusstatement(self, cur: mparser.UMinusNode) -> T.Union[int, Disabler]:
        v = self.evaluate_statement(cur.value)
        if isinstance(v, Disabler):
            return v
        if not isinstance(v, int):
            raise InterpreterException('Argument to negation is not an integer.')
        return -v

    @FeatureNew('/ with string arguments', '0.49.0')
    def evaluate_path_join(self, l: str, r: str) -> str:
        if not isinstance(l, str):
            raise InvalidCode('The division operator can only append to a string.')
        if not isinstance(r, str):
            raise InvalidCode('The division operator can only append a string.')
        return self.join_path_strings((l, r))

    def evaluate_division(self, l: T.Any, r: T.Any) -> T.Union[int, str]:
        if isinstance(l, str) or isinstance(r, str):
            return self.evaluate_path_join(l, r)
        if isinstance(l, int) and isinstance(r, int):
            if r == 0:
                raise InvalidCode('Division by zero.')
            return l // r
        raise InvalidCode('Division works only with strings or integers.')

    def evaluate_arithmeticstatement(self, cur: mparser.ArithmeticNode) -> T.Union[int, str, dict, list, Disabler]:
        l = self.evaluate_statement(cur.left)
        if isinstance(l, Disabler):
            return l
        r = self.evaluate_statement(cur.right)
        if isinstance(r, Disabler):
            return r

        if cur.operation == 'add':
            if isinstance(l, dict) and isinstance(r, dict):
                return {**l, **r}
            try:
                # MyPy error due to handling two Unions (we are catching all exceptions anyway)
                return l + r  # type: ignore
            except Exception as e:
                raise InvalidCode('Invalid use of addition: ' + str(e))
        elif cur.operation == 'sub':
            if not isinstance(l, int) or not isinstance(r, int):
                raise InvalidCode('Subtraction works only with integers.')
            return l - r
        elif cur.operation == 'mul':
            if not isinstance(l, int) or not isinstance(r, int):
                raise InvalidCode('Multiplication works only with integers.')
            return l * r
        elif cur.operation == 'div':
            return self.evaluate_division(l, r)
        elif cur.operation == 'mod':
            if not isinstance(l, int) or not isinstance(r, int):
                raise InvalidCode('Modulo works only with integers.')
            return l % r
        else:
            raise InvalidCode('You broke me.')

    def evaluate_ternary(self, node: mparser.TernaryNode) -> TYPE_var:
        assert(isinstance(node, mparser.TernaryNode))
        result = self.evaluate_statement(node.condition)
        if isinstance(result, Disabler):
            return result
        if not isinstance(result, bool):
            raise InterpreterException('Ternary condition is not boolean.')
        if result:
            return self.evaluate_statement(node.trueblock)
        else:
            return self.evaluate_statement(node.falseblock)

    @FeatureNew('format strings', '0.58.0')
    def evaluate_fstring(self, node: mparser.FormatStringNode) -> TYPE_var:
        assert(isinstance(node, mparser.FormatStringNode))

        def replace(match: T.Match[str]) -> str:
            var = str(match.group(1))
            try:
                val = self.variables[var]
                if not isinstance(val, (str, int, float, bool)):
                    raise InvalidCode(f'Identifier "{var}" does not name a formattable variable ' +
                        '(has to be an integer, a string, a floating point number or a boolean).')

                return str(val)
            except KeyError:
                raise InvalidCode(f'Identifier "{var}" does not name a variable.')

        return re.sub(r'@([_a-zA-Z][_0-9a-zA-Z]*)@', replace, node.value)

    def evaluate_foreach(self, node: mparser.ForeachClauseNode) -> None:
        assert(isinstance(node, mparser.ForeachClauseNode))
        items = self.evaluate_statement(node.items)

        if isinstance(items, (list, RangeHolder)):
            if len(node.varnames) != 1:
                raise InvalidArguments('Foreach on array does not unpack')
            varname = node.varnames[0]
            for item in items:
                self.set_variable(varname, item)
                try:
                    self.evaluate_codeblock(node.block)
                except ContinueRequest:
                    continue
                except BreakRequest:
                    break
        elif isinstance(items, dict):
            if len(node.varnames) != 2:
                raise InvalidArguments('Foreach on dict unpacks key and value')
            for key, value in sorted(items.items()):
                self.set_variable(node.varnames[0], key)
                self.set_variable(node.varnames[1], value)
                try:
                    self.evaluate_codeblock(node.block)
                except ContinueRequest:
                    continue
                except BreakRequest:
                    break
        else:
            raise InvalidArguments('Items of foreach loop must be an array or a dict')

    def evaluate_plusassign(self, node: mparser.PlusAssignmentNode) -> None:
        assert(isinstance(node, mparser.PlusAssignmentNode))
        varname = node.var_name
        addition = self.evaluate_statement(node.value)

        # Remember that all variables are immutable. We must always create a
        # full new variable and then assign it.
        old_variable = self.get_variable(varname)
        new_value = None  # type: T.Union[str, int, float, bool, dict, list]
        if isinstance(old_variable, str):
            if not isinstance(addition, str):
                raise InvalidArguments('The += operator requires a string on the right hand side if the variable on the left is a string')
            new_value = old_variable + addition
        elif isinstance(old_variable, int):
            if not isinstance(addition, int):
                raise InvalidArguments('The += operator requires an int on the right hand side if the variable on the left is an int')
            new_value = old_variable + addition
        elif isinstance(old_variable, list):
            if isinstance(addition, list):
                new_value = old_variable + addition
            else:
                new_value = old_variable + [addition]
        elif isinstance(old_variable, dict):
            if not isinstance(addition, dict):
                raise InvalidArguments('The += operator requires a dict on the right hand side if the variable on the left is a dict')
            new_value = {**old_variable, **addition}
        # Add other data types here.
        else:
            raise InvalidArguments('The += operator currently only works with arrays, dicts, strings or ints')
        self.set_variable(varname, new_value)

    def evaluate_indexing(self, node: mparser.IndexNode) -> TYPE_var:
        assert(isinstance(node, mparser.IndexNode))
        iobject = self.evaluate_statement(node.iobject)
        if isinstance(iobject, Disabler):
            return iobject
        if not hasattr(iobject, '__getitem__'):
            raise InterpreterException(
                'Tried to index an object that doesn\'t support indexing.')
        index = self.evaluate_statement(node.index)

        if isinstance(iobject, dict):
            if not isinstance(index, str):
                raise InterpreterException('Key is not a string')
            try:
                # The cast is required because we don't have recursive types...
                return T.cast(TYPE_var, iobject[index])
            except KeyError:
                raise InterpreterException('Key %s is not in dict' % index)
        else:
            if not isinstance(index, int):
                raise InterpreterException('Index value is not an integer.')
            try:
                # Ignore the MyPy error, since we don't know all indexable types here
                # and we handle non indexable types with an exception
                # TODO maybe find a better solution
                return iobject[index]  # type: ignore
            except IndexError:
                # We are already checking for the existence of __getitem__, so this should be save
                raise InterpreterException('Index %d out of bounds of array of size %d.' % (index, len(iobject)))  # type: ignore

    def function_call(self, node: mparser.FunctionNode) -> T.Optional[TYPE_var]:
        func_name = node.func_name
        (posargs, kwargs) = self.reduce_arguments(node.args)
        if is_disabled(posargs, kwargs) and func_name not in {'get_variable', 'set_variable', 'is_disabler'}:
            return Disabler()
        if func_name in self.funcs:
            func = self.funcs[func_name]
            func_args = posargs  # type: T.Any
            if not getattr(func, 'no-args-flattening', False):
                func_args = flatten(posargs)
            return func(node, func_args, kwargs)
        else:
            self.unknown_function_called(func_name)
            return None

    def method_call(self, node: mparser.MethodNode) -> TYPE_var:
        invokable = node.source_object
        if isinstance(invokable, mparser.IdNode):
            object_name = invokable.value
            obj = self.get_variable(object_name)
        else:
            obj = self.evaluate_statement(invokable)
        method_name = node.name
        (args, kwargs) = self.reduce_arguments(node.args)
        if is_disabled(args, kwargs):
            return Disabler()
        if isinstance(obj, str):
            return self.string_method_call(obj, method_name, args, kwargs)
        if isinstance(obj, bool):
            return self.bool_method_call(obj, method_name, args, kwargs)
        if isinstance(obj, int):
            return self.int_method_call(obj, method_name, args, kwargs)
        if isinstance(obj, list):
            return self.array_method_call(obj, method_name, args, kwargs)
        if isinstance(obj, dict):
            return self.dict_method_call(obj, method_name, args, kwargs)
        if isinstance(obj, mesonlib.File):
            raise InvalidArguments('File object "%s" is not callable.' % obj)
        if not isinstance(obj, InterpreterObject):
            raise InvalidArguments('Variable "%s" is not callable.' % object_name)
        # Special case. This is the only thing you can do with a disabler
        # object. Every other use immediately returns the disabler object.
        if isinstance(obj, Disabler):
            if method_name == 'found':
                return False
            else:
                return Disabler()
        if method_name == 'extract_objects':
            if not isinstance(obj, ObjectHolder):
                raise InvalidArguments(f'Invalid operation "extract_objects" on variable "{object_name}"')
            self.validate_extraction(obj.held_object)
        obj.current_node = node
        return obj.method_call(method_name, args, kwargs)

    @builtinMethodNoKwargs
    def bool_method_call(self, obj: bool, method_name: str, posargs: T.List[TYPE_nvar], kwargs: T.Dict[str, T.Any]) -> T.Union[str, int]:
        if method_name == 'to_string':
            if not posargs:
                if obj:
                    return 'true'
                else:
                    return 'false'
            elif len(posargs) == 2 and isinstance(posargs[0], str) and isinstance(posargs[1], str):
                if obj:
                    return posargs[0]
                else:
                    return posargs[1]
            else:
                raise InterpreterException('bool.to_string() must have either no arguments or exactly two string arguments that signify what values to return for true and false.')
        elif method_name == 'to_int':
            if obj:
                return 1
            else:
                return 0
        else:
            raise InterpreterException('Unknown method "%s" for a boolean.' % method_name)

    @builtinMethodNoKwargs
    def int_method_call(self, obj: int, method_name: str, posargs: T.List[TYPE_nvar], kwargs: T.Dict[str, T.Any]) -> T.Union[str, bool]:
        if method_name == 'is_even':
            if not posargs:
                return obj % 2 == 0
            else:
                raise InterpreterException('int.is_even() must have no arguments.')
        elif method_name == 'is_odd':
            if not posargs:
                return obj % 2 != 0
            else:
                raise InterpreterException('int.is_odd() must have no arguments.')
        elif method_name == 'to_string':
            if not posargs:
                return str(obj)
            else:
                raise InterpreterException('int.to_string() must have no arguments.')
        else:
            raise InterpreterException('Unknown method "%s" for an integer.' % method_name)

    @staticmethod
    def _get_one_string_posarg(posargs: T.List[TYPE_nvar], method_name: str) -> str:
        if len(posargs) > 1:
            m = '{}() must have zero or one arguments'
            raise InterpreterException(m.format(method_name))
        elif len(posargs) == 1:
            s = posargs[0]
            if not isinstance(s, str):
                m = '{}() argument must be a string'
                raise InterpreterException(m.format(method_name))
            return s
        return None

    @builtinMethodNoKwargs
    def string_method_call(self, obj: str, method_name: str, posargs: T.List[TYPE_nvar], kwargs: T.Dict[str, T.Any]) -> T.Union[str, int, bool, T.List[str]]:
        if method_name == 'strip':
            s1 = self._get_one_string_posarg(posargs, 'strip')
            if s1 is not None:
                return obj.strip(s1)
            return obj.strip()
        elif method_name == 'format':
            return self.format_string(obj, posargs)
        elif method_name == 'to_upper':
            return obj.upper()
        elif method_name == 'to_lower':
            return obj.lower()
        elif method_name == 'underscorify':
            return re.sub(r'[^a-zA-Z0-9]', '_', obj)
        elif method_name == 'split':
            s2 = self._get_one_string_posarg(posargs, 'split')
            if s2 is not None:
                return obj.split(s2)
            return obj.split()
        elif method_name == 'startswith' or method_name == 'contains' or method_name == 'endswith':
            s3 = posargs[0]
            if not isinstance(s3, str):
                raise InterpreterException('Argument must be a string.')
            if method_name == 'startswith':
                return obj.startswith(s3)
            elif method_name == 'contains':
                return obj.find(s3) >= 0
            return obj.endswith(s3)
        elif method_name == 'to_int':
            try:
                return int(obj)
            except Exception:
                raise InterpreterException(f'String {obj!r} cannot be converted to int')
        elif method_name == 'join':
            if len(posargs) != 1:
                raise InterpreterException('Join() takes exactly one argument.')
            strlist = posargs[0]
            check_stringlist(strlist)
            assert isinstance(strlist, list)  # Required for mypy
            return obj.join(strlist)
        elif method_name == 'version_compare':
            if len(posargs) != 1:
                raise InterpreterException('Version_compare() takes exactly one argument.')
            cmpr = posargs[0]
            if not isinstance(cmpr, str):
                raise InterpreterException('Version_compare() argument must be a string.')
            if isinstance(obj, MesonVersionString):
                self.tmp_meson_version = cmpr
            return mesonlib.version_compare(obj, cmpr)
        elif method_name == 'substring':
            if len(posargs) > 2:
                raise InterpreterException('substring() takes maximum two arguments.')
            start = 0
            end = len(obj)
            if len (posargs) > 0:
                if not isinstance(posargs[0], int):
                    raise InterpreterException('substring() argument must be an int')
                start = posargs[0]
            if len (posargs) > 1:
                if not isinstance(posargs[1], int):
                    raise InterpreterException('substring() argument must be an int')
                end = posargs[1]
            return obj[start:end]
        elif method_name == 'replace':
            FeatureNew.single_use('str.replace', '0.58.0', self.subproject)
            if len(posargs) != 2:
                raise InterpreterException('replace() takes exactly two arguments.')
            if not isinstance(posargs[0], str) or not isinstance(posargs[1], str):
                raise InterpreterException('replace() requires that both arguments be strings')
            return obj.replace(posargs[0], posargs[1])
        raise InterpreterException('Unknown method "%s" for a string.' % method_name)

    def format_string(self, templ: str, args: T.List[TYPE_nvar]) -> str:
        arg_strings = []
        for arg in args:
            if isinstance(arg, mparser.BaseNode):
                arg = self.evaluate_statement(arg)
            if isinstance(arg, bool): # Python boolean is upper case.
                arg = str(arg).lower()
            arg_strings.append(str(arg))

        def arg_replace(match: T.Match[str]) -> str:
            idx = int(match.group(1))
            if idx >= len(arg_strings):
                raise InterpreterException(f'Format placeholder @{idx}@ out of range.')
            return arg_strings[idx]

        return re.sub(r'@(\d+)@', arg_replace, templ)

    def unknown_function_called(self, func_name: str) -> None:
        raise InvalidCode('Unknown function "%s".' % func_name)

    @builtinMethodNoKwargs
    def array_method_call(self, obj: T.List[TYPE_var], method_name: str, posargs: T.List[TYPE_nvar], kwargs: T.Dict[str, T.Any]) -> TYPE_var:
        if method_name == 'contains':
            def check_contains(el: list) -> bool:
                if len(posargs) != 1:
                    raise InterpreterException('Contains method takes exactly one argument.')
                item = posargs[0]
                for element in el:
                    if isinstance(element, list):
                        found = check_contains(element)
                        if found:
                            return True
                    if element == item:
                        return True
                return False
            return check_contains(obj)
        elif method_name == 'length':
            return len(obj)
        elif method_name == 'get':
            index = posargs[0]
            fallback = None
            if len(posargs) == 2:
                fallback = posargs[1]
            elif len(posargs) > 2:
                m = 'Array method \'get()\' only takes two arguments: the ' \
                    'index and an optional fallback value if the index is ' \
                    'out of range.'
                raise InvalidArguments(m)
            if not isinstance(index, int):
                raise InvalidArguments('Array index must be a number.')
            if index < -len(obj) or index >= len(obj):
                if fallback is None:
                    m = 'Array index {!r} is out of bounds for array of size {!r}.'
                    raise InvalidArguments(m.format(index, len(obj)))
                if isinstance(fallback, mparser.BaseNode):
                    return self.evaluate_statement(fallback)
                return fallback
            return obj[index]
        m = 'Arrays do not have a method called {!r}.'
        raise InterpreterException(m.format(method_name))

    @builtinMethodNoKwargs
    def dict_method_call(self, obj: T.Dict[str, TYPE_var], method_name: str, posargs: T.List[TYPE_nvar], kwargs: T.Dict[str, T.Any]) -> TYPE_var:
        if method_name in ('has_key', 'get'):
            if method_name == 'has_key':
                if len(posargs) != 1:
                    raise InterpreterException('has_key() takes exactly one argument.')
            else:
                if len(posargs) not in (1, 2):
                    raise InterpreterException('get() takes one or two arguments.')

            key = posargs[0]
            if not isinstance(key, (str)):
                raise InvalidArguments('Dictionary key must be a string.')

            has_key = key in obj

            if method_name == 'has_key':
                return has_key

            if has_key:
                return obj[key]

            if len(posargs) == 2:
                fallback = posargs[1]
                if isinstance(fallback, mparser.BaseNode):
                    return self.evaluate_statement(fallback)
                return fallback

            raise InterpreterException(f'Key {key!r} is not in the dictionary.')

        if method_name == 'keys':
            if len(posargs) != 0:
                raise InterpreterException('keys() takes no arguments.')
            return sorted(obj.keys())

        raise InterpreterException('Dictionaries do not have a method called "%s".' % method_name)

    def reduce_arguments(
                self,
                args: mparser.ArgumentNode,
                key_resolver: T.Callable[[mparser.BaseNode], str] = default_resolve_key,
                duplicate_key_error: T.Optional[str] = None,
            ) -> T.Tuple[T.List[TYPE_nvar], TYPE_nkwargs]:
        assert(isinstance(args, mparser.ArgumentNode))
        if args.incorrect_order():
            raise InvalidArguments('All keyword arguments must be after positional arguments.')
        self.argument_depth += 1
        reduced_pos = [self.evaluate_statement(arg) for arg in args.arguments]  # type: T.List[TYPE_nvar]
        reduced_kw = {}  # type: TYPE_nkwargs
        for key, val in args.kwargs.items():
            reduced_key = key_resolver(key)
            reduced_val = val  # type: TYPE_nvar
            if isinstance(reduced_val, mparser.BaseNode):
                reduced_val = self.evaluate_statement(reduced_val)
            if duplicate_key_error and reduced_key in reduced_kw:
                raise InvalidArguments(duplicate_key_error.format(reduced_key))
            reduced_kw[reduced_key] = reduced_val
        self.argument_depth -= 1
        final_kw = self.expand_default_kwargs(reduced_kw)
        return reduced_pos, final_kw

    def expand_default_kwargs(self, kwargs: TYPE_nkwargs) -> TYPE_nkwargs:
        if 'kwargs' not in kwargs:
            return kwargs
        to_expand = kwargs.pop('kwargs')
        if not isinstance(to_expand, dict):
            raise InterpreterException('Value of "kwargs" must be dictionary.')
        if 'kwargs' in to_expand:
            raise InterpreterException('Kwargs argument must not contain a "kwargs" entry. Points for thinking meta, though. :P')
        for k, v in to_expand.items():
            if k in kwargs:
                raise InterpreterException(f'Entry "{k}" defined both as a keyword argument and in a "kwarg" entry.')
            kwargs[k] = v
        return kwargs

    def assignment(self, node: mparser.AssignmentNode) -> None:
        assert(isinstance(node, mparser.AssignmentNode))
        if self.argument_depth != 0:
            raise InvalidArguments('''Tried to assign values inside an argument list.
To specify a keyword argument, use : instead of =.''')
        var_name = node.var_name
        if not isinstance(var_name, str):
            raise InvalidArguments('Tried to assign value to a non-variable.')
        value = self.evaluate_statement(node.value)
        if not self.is_assignable(value):
            raise InvalidCode('Tried to assign an invalid value to variable.')
        # For mutable objects we need to make a copy on assignment
        if isinstance(value, MutableInterpreterObject):
            value = copy.deepcopy(value)
        self.set_variable(var_name, value)
        return None

    def set_variable(self, varname: str, variable: TYPE_var) -> None:
        if variable is None:
            raise InvalidCode('Can not assign None to variable.')
        if not isinstance(varname, str):
            raise InvalidCode('First argument to set_variable must be a string.')
        if not self.is_assignable(variable):
            raise InvalidCode('Assigned value not of assignable type.')
        if re.match('[_a-zA-Z][_0-9a-zA-Z]*$', varname) is None:
            raise InvalidCode('Invalid variable name: ' + varname)
        if varname in self.builtin:
            raise InvalidCode('Tried to overwrite internal variable "%s"' % varname)
        self.variables[varname] = variable

    def get_variable(self, varname: str) -> TYPE_var:
        if varname in self.builtin:
            return self.builtin[varname]
        if varname in self.variables:
            return self.variables[varname]
        raise InvalidCode('Unknown variable "%s".' % varname)

    def is_assignable(self, value: T.Any) -> bool:
        return isinstance(value, (InterpreterObject, dependencies.Dependency,
                                  str, int, list, dict, mesonlib.File))

    def validate_extraction(self, buildtarget: InterpreterObject) -> None:
        raise InterpreterException('validate_extraction is not implemented in this context (please file a bug)')
