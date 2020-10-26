#!/usr/bin/env python3

# Copyright (c) Facebook, Inc. and its affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.
import dataclasses
import os
import unittest
from datetime import datetime
from typing import Dict, Iterable, List, Optional
from unittest.mock import MagicMock

from torchelastic.tsm.driver.api import (
    _TERMINAL_STATES,
    AppDryRunInfo,
    AppHandle,
    Application,
    AppState,
    AppStatus,
    Container,
    DescribeAppResponse,
    ElasticRole,
    InvalidRunConfigException,
    MalformedAppHandleException,
    Resources,
    Role,
    RunConfig,
    Scheduler,
    SchedulerBackend,
    Session,
    macros,
    make_app_handle,
    parse_app_handle,
    runopts,
)


class ApplicationStatusTest(unittest.TestCase):
    def test_is_terminal(self):
        for s in AppState:
            is_terminal = AppStatus(state=s).is_terminal()
            if s in _TERMINAL_STATES:
                self.assertTrue(is_terminal)
            else:
                self.assertFalse(is_terminal)


class ResourcesTest(unittest.TestCase):
    def test_copy_resources(self):
        old_capabilities = {"test_key": "test_value", "old_key": "old_value"}
        resources = Resources(1, 2, 3, old_capabilities)
        new_resources = Resources.copy(
            resources, test_key="test_value_new", new_key="new_value"
        )
        self.assertEqual(new_resources.cpu, 1)
        self.assertEqual(new_resources.gpu, 2)
        self.assertEqual(new_resources.memMB, 3)
        self.assertEqual(len(new_resources.capabilities), 3)
        self.assertEqual(new_resources.capabilities["old_key"], "old_value")
        self.assertEqual(new_resources.capabilities["test_key"], "test_value_new")
        self.assertEqual(new_resources.capabilities["new_key"], "new_value")
        self.assertEqual(resources.capabilities["test_key"], "test_value")


class RoleBuilderTest(unittest.TestCase):
    def test_build_role(self):
        # runs: ENV_VAR_1=FOOBAR /bin/echo hello world
        container = Container(image="test_image")
        container.ports(foo=8080)
        trainer = (
            Role("trainer")
            .runs("/bin/echo", "hello", "world", ENV_VAR_1="FOOBAR")
            .on(container)
            .replicas(2)
        )

        self.assertEqual("trainer", trainer.name)
        self.assertEqual("/bin/echo", trainer.entrypoint)
        self.assertEqual({"ENV_VAR_1": "FOOBAR"}, trainer.env)
        self.assertEqual(["hello", "world"], trainer.args)
        self.assertEqual(container, trainer.container)
        self.assertEqual(2, trainer.num_replicas)


class ElasticRoleBuilderTest(unittest.TestCase):
    def test_build_elastic_role(self):
        # runs: python -m torchelastic.distributed.launch
        #                    --nnodes 2:4
        #                    --max_restarts 3
        #                    --no_python True
        #                    --rdzv_backend etcd
        #                    --rdzv_id ${app_id}
        #                    /bin/echo hello world
        container = Container(image="test_image")
        container.ports(foo=8080)
        elastic_trainer = (
            ElasticRole("elastic_trainer", nnodes="2:4", max_restarts=3, no_python=True)
            .runs("/bin/echo", "hello", "world", ENV_VAR_1="FOOBAR")
            .on(container)
            .replicas(2)
        )
        self.assertEqual("elastic_trainer", elastic_trainer.name)
        self.assertEqual("python", elastic_trainer.entrypoint)
        self.assertEqual(
            [
                "-m",
                "torchelastic.distributed.launch",
                "--nnodes",
                "2:4",
                "--max_restarts",
                "3",
                "--no_python",
                "--rdzv_backend",
                "etcd",
                "--rdzv_id",
                macros.app_id,
                "--role",
                "elastic_trainer",
                "/bin/echo",
                "hello",
                "world",
            ],
            elastic_trainer.args,
        )
        self.assertEqual({"ENV_VAR_1": "FOOBAR"}, elastic_trainer.env)
        self.assertEqual(container, elastic_trainer.container)
        self.assertEqual(2, elastic_trainer.num_replicas)

    def test_build_elastic_role_override_rdzv_params(self):
        role = ElasticRole(
            "test_role", nnodes="2:4", rdzv_backend="zeus", rdzv_id="foobar"
        ).runs("user_script.py", "--script_arg", "foo")
        self.assertEqual(
            [
                "-m",
                "torchelastic.distributed.launch",
                "--nnodes",
                "2:4",
                "--rdzv_backend",
                "zeus",
                "--rdzv_id",
                "foobar",
                "--role",
                "test_role",
                os.path.join(macros.img_root, "user_script.py"),
                "--script_arg",
                "foo",
            ],
            role.args,
        )

    def test_build_elastic_role_flag_args(self):
        role = ElasticRole("test_role", no_python=False).runs("user_script.py")
        self.assertEqual(
            [
                "-m",
                "torchelastic.distributed.launch",
                "--rdzv_backend",
                "etcd",
                "--rdzv_id",
                macros.app_id,
                "--role",
                "test_role",
                os.path.join(macros.img_root, "user_script.py"),
            ],
            role.args,
        )

    def test_build_elastic_role_img_root_already_in_entrypoint(self):
        role = ElasticRole("test_role", no_python=False).runs(
            os.path.join(macros.img_root, "user_script.py")
        )
        self.assertEqual(
            [
                "-m",
                "torchelastic.distributed.launch",
                "--rdzv_backend",
                "etcd",
                "--rdzv_id",
                macros.app_id,
                "--role",
                "test_role",
                os.path.join(macros.img_root, "user_script.py"),
            ],
            role.args,
        )


class AppHandleTest(unittest.TestCase):
    def test_parse_malformed_app_handles(self):
        bad_app_handles = {
            "my_session/my_application_id": "missing scheduler backend",
            "local://my_session/": "missing app_id",
            "local://my_application_id": "missing session",
        }

        for handle, msg in bad_app_handles.items():
            with self.subTest(f"malformed app handle: {msg}", handle=handle):
                with self.assertRaises(MalformedAppHandleException):
                    parse_app_handle(handle)

    def test_parse(self):
        (scheduler_backend, session_name, app_id) = parse_app_handle(
            "local://my_session/my_app_id_1234"
        )
        self.assertEqual("local", scheduler_backend)
        self.assertEqual("my_session", session_name)
        self.assertEqual("my_app_id_1234", app_id)

    def test_make(self):
        app_handle = make_app_handle(
            scheduler_backend="local",
            session_name="my_session",
            app_id="my_app_id_1234",
        )
        self.assertEqual("local://my_session/my_app_id_1234", app_handle)


class ApplicationTest(unittest.TestCase):
    def test_application(self):
        container = Container(image="test_image")
        trainer = Role("trainer").runs("/bin/sleep", "10").on(container).replicas(2)
        app = Application(name="test_app").of(trainer)
        self.assertEqual("test_app", app.name)
        self.assertEqual(1, len(app.roles))
        self.assertEqual(trainer, app.roles[0])

    def test_application_default(self):
        app = Application(name="test_app")
        self.assertEqual(0, len(app.roles))


class SessionTest(unittest.TestCase):
    class MockSession(Session):
        def __init__(self):
            super().__init__("mock session")

        def _run(
            self, app: Application, scheduler: SchedulerBackend, cfg: RunConfig
        ) -> str:
            return app.name

        def status(self, app_handle: AppHandle) -> Optional[AppStatus]:
            return None

        def wait(self, app_handle: AppHandle) -> Optional[AppStatus]:
            return None

        def list(self) -> Dict[str, Application]:
            return {}

        def stop(self, app_handle: AppHandle) -> None:
            pass

        def describe(self, app_handle: AppHandle) -> Optional[Application]:
            return Application(app_handle)

        def log_lines(
            self,
            app_handle: AppHandle,
            role_name: str,
            k: int = 0,
            regex: Optional[str] = None,
            since: Optional[datetime] = None,
            until: Optional[datetime] = None,
        ) -> Iterable:
            return iter([])

        def scheduler_backends(self) -> List[SchedulerBackend]:
            return ["default"]

    def test_validate_no_roles(self):
        session = self.MockSession()
        with self.assertRaises(ValueError):
            app = Application("no roles")
            session.run(app)

    def test_validate_no_container(self):
        session = self.MockSession()
        with self.assertRaises(ValueError):
            role = Role("no container").runs("echo", "hello_world")
            app = Application("no container").of(role)
            session.run(app)

    def test_validate_no_resource(self):
        session = self.MockSession()
        with self.assertRaises(ValueError):
            container = Container("no resource")
            role = Role("no resource").runs("echo", "hello_world").on(container)
            app = Application("no resource").of(role)
            session.run(app)

    def test_validate_invalid_replicas(self):
        session = self.MockSession()
        with self.assertRaises(ValueError):
            container = Container("torch").require(Resources(cpu=1, gpu=0, memMB=500))
            role = (
                Role("no container")
                .runs("echo", "hello_world")
                .on(container)
                .replicas(0)
            )
            app = Application("no container").of(role)
            session.run(app)


class AppDryRunInfoTest(unittest.TestCase):
    def test_repr(self):
        request_mock = MagicMock()
        to_string_mock = MagicMock()
        info = AppDryRunInfo(request_mock, to_string_mock)
        info.__repr__()
        self.assertEqual(request_mock, info.request)

        to_string_mock.assert_called_once_with(request_mock)


class RunConfigTest(unittest.TestCase):
    def get_cfg(self):
        cfg = RunConfig()
        cfg.set("run_as", "root")
        cfg.set("cluster_id", 123)
        cfg.set("priority", 0.5)
        cfg.set("preemtible", True)
        return cfg

    def test_valid_values(self):
        cfg = self.get_cfg()

        self.assertEqual("root", cfg.get("run_as"))
        self.assertEqual(123, cfg.get("cluster_id"))
        self.assertEqual(0.5, cfg.get("priority"))
        self.assertTrue(cfg.get("preemtible"))
        self.assertIsNone(cfg.get("unknown"))

    def test_serde(self):
        """
        tests trivial serialization into dict then back
        """
        cfg = self.get_cfg()
        ser = dataclasses.asdict(cfg)
        deser = RunConfig(**ser)

        self.assertEqual("root", deser.get("run_as"))
        self.assertEqual(123, deser.get("cluster_id"))
        self.assertEqual(0.5, deser.get("priority"))
        self.assertTrue(deser.get("preemtible"))

    def test_runopts_add(self):
        """
        tests for various add option variations
        does not assert anything, a successful test
        should not raise any unexpected errors
        """
        opts = runopts()
        opts.add("run_as", type_=str, help="run as user")
        opts.add("run_as_default", type_=str, help="run as user", default="root")
        opts.add("run_as_required", type_=str, help="run as user", required=True)

        with self.assertRaises(ValueError):
            opts.add(
                "run_as", type_=str, help="run as user", default="root", required=True
            )

        opts.add("priority", type_=int, help="job priority", default=10)

        with self.assertRaises(TypeError):
            opts.add("priority", type_=int, help="job priority", default=0.5)

        # this print is intentional (demonstrates the intended usecase)
        print(opts)

    def get_runopts(self):
        opts = runopts()
        opts.add("run_as", type_=str, help="run as user", required=True)
        opts.add("priority", type_=int, help="job priority", default=10)
        opts.add("cluster_id", type_=str, help="cluster to submit job")
        return opts

    def test_runopts_resolve_minimal(self):
        opts = self.get_runopts()

        cfg = RunConfig()
        cfg.set("run_as", "foobar")

        resolved = opts.resolve(cfg)
        self.assertEqual("foobar", resolved.get("run_as"))
        self.assertEqual(10, resolved.get("priority"))
        self.assertIsNone(resolved.get("cluster_id"))

        # make sure original config is untouched
        self.assertEqual("foobar", cfg.get("run_as"))
        self.assertIsNone(cfg.get("priority"))
        self.assertIsNone(cfg.get("cluster_id"))

    def test_runopts_resolve_override(self):
        opts = self.get_runopts()

        cfg = RunConfig()
        cfg.set("run_as", "foobar")
        cfg.set("priority", 20)
        cfg.set("cluster_id", "test_cluster")

        resolved = opts.resolve(cfg)
        self.assertEqual("foobar", resolved.get("run_as"))
        self.assertEqual(20, resolved.get("priority"))
        self.assertEqual("test_cluster", resolved.get("cluster_id"))

    def test_runopts_resolve_missing_required(self):
        opts = self.get_runopts()

        cfg = RunConfig()
        cfg.set("priority", 20)
        cfg.set("cluster_id", "test_cluster")

        with self.assertRaises(InvalidRunConfigException):
            opts.resolve(cfg)

    def test_runopts_resolve_bad_type(self):
        opts = self.get_runopts()

        cfg = RunConfig()
        cfg.set("run_as", "foobar")
        cfg.set("cluster_id", 123)

        with self.assertRaises(InvalidRunConfigException):
            opts.resolve(cfg)

    def test_runopts_resolve_unioned(self):
        # runconfigs is a union of all run opts for all schedulers
        # make sure  opts resolves run configs that have more
        # configs than it knows about
        opts = self.get_runopts()

        cfg = RunConfig()
        cfg.set("run_as", "foobar")
        cfg.set("some_other_opt", "baz")

        resolved = opts.resolve(cfg)
        self.assertEqual("foobar", resolved.get("run_as"))
        self.assertEqual(10, resolved.get("priority"))
        self.assertIsNone(resolved.get("cluster_id"))
        self.assertEqual("baz", resolved.get("some_other_opt"))


class SchedulerTest(unittest.TestCase):
    class MockScheduler(Scheduler):
        def _submit(self, app: Application, cfg: RunConfig) -> str:
            return app.name

        def _submit_dryrun(self, app: Application, cfg: RunConfig) -> AppDryRunInfo:
            return AppDryRunInfo(None, lambda t: "None")

        def describe(self, app_id: str) -> Optional[DescribeAppResponse]:
            return None

        def _cancel_existing(self, app_id: str) -> None:
            pass

        def log_iter(
            self,
            app_id: str,
            role_name: str,
            k: int = 0,
            regex: Optional[str] = None,
            since: Optional[datetime] = None,
            until: Optional[datetime] = None,
        ) -> Iterable:
            return iter([])

        def run_opts(self) -> runopts:
            opts = runopts()
            opts.add("foo", type_=str, required=True, help="required option")
            return opts

    def test_invalid_run_cfg(self):
        scheduler_mock = SchedulerTest.MockScheduler()
        app_mock = MagicMock()

        with self.assertRaises(InvalidRunConfigException):
            empty_cfg = RunConfig()
            scheduler_mock.submit(app_mock, empty_cfg)

        with self.assertRaises(InvalidRunConfigException):
            bad_type_cfg = RunConfig()
            bad_type_cfg.set("foo", 100)
            scheduler_mock.submit(app_mock, empty_cfg)

    def test_invalid_dryrun_cfg(self):
        scheduler_mock = SchedulerTest.MockScheduler()
        app_mock = MagicMock()

        with self.assertRaises(InvalidRunConfigException):
            empty_cfg = RunConfig()
            scheduler_mock.submit_dryrun(app_mock, empty_cfg)

        with self.assertRaises(InvalidRunConfigException):
            bad_type_cfg = RunConfig()
            bad_type_cfg.set("foo", 100)
            scheduler_mock.submit_dryrun(app_mock, empty_cfg)
