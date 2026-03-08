import json
import unittest

from unittest.mock import patch
from wings.core.config_loader import _set_kv_cache_config


class TestKvCacheConfig(unittest.TestCase):
    def test_set_kv_cache_config(self):
        # 场景1: 同时启用LMCache和PD角色（Ascend设备）
        test_params = {}
        with patch("wings.core.config_loader.get_lmcache_env", return_value=True) as mock_lmc, \
             patch("wings.core.config_loader.get_pd_role_env", return_value="P") as mock_pd:
            _set_kv_cache_config(test_params, {"device": "ascend", "device_count": 2})
        self.assertIn("kv_transfer_config", test_params)
        config = json.loads(test_params.get("kv_transfer_config"))
        self.assertEqual(config["kv_connector"], "MultiConnector")
        self.assertEqual(len(config["kv_connector_extra_config"]["connectors"]), 2)
        self.assertEqual(
            config["kv_connector_extra_config"]["connectors"][0]["kv_connector"],
            "LLMDataDistCMgrConnector"
        )
        self.assertEqual(
            config["kv_connector_extra_config"]["connectors"][1]["kv_connector"],
            "LMCacheConnectorV1"
        )

        # 场景2: 仅启用LMCache（NVIDIA设备）
        test_params = {}
        with patch("wings.core.config_loader.get_lmcache_env", return_value=True):
            _set_kv_cache_config(test_params, {"device": "nvidia"})
        config = json.loads(test_params.get("kv_transfer_config"))
        self.assertEqual(config["kv_connector"], "LMCacheConnectorV1")
        self.assertNotIn("kv_parallel_size", config)

        # 场景3: 仅启用LMCache（Ascend设备）
        test_params = {}
        with patch("wings.core.config_loader.get_lmcache_env", return_value=True):
            _set_kv_cache_config(test_params, {"device": "ascend", "device_count": 1})
        config = json.loads(test_params.get("kv_transfer_config"))
        self.assertEqual(config["kv_connector"], "LMCacheConnectorV1")
        self.assertNotIn("kv_parallel_size", config)

        # 场景4: 仅启用PD角色（Ascend设备）
        test_params = {}
        with patch("wings.core.config_loader.get_pd_role_env", return_value="D"):
            _set_kv_cache_config(test_params, {"device": "ascend", "device_count": 4})
        config = json.loads(test_params.get("kv_transfer_config"))
        self.assertEqual(config["kv_connector"], "LLMDataDistCMgrConnector")
        self.assertEqual(config["kv_parallel_size"], 4)
        self.assertEqual(config["kv_buffer_device"], "npu")

        # 场景5: 仅启用PD角色（NVIDIA设备）
        test_params = {}
        with patch("wings.core.config_loader.get_pd_role_env", return_value="P"):
            _set_kv_cache_config(test_params, {"device": "nvidia"})
        config = json.loads(test_params.get("kv_transfer_config"))
        self.assertEqual(config["kv_connector"], "NixlConnector")
        self.assertEqual(config["kv_role"], "kv_both")

        # 场景6: 无效PD角色
        test_params = {}
        with patch("wings.core.config_loader.get_pd_role_env", return_value="X"):
            _set_kv_cache_config(test_params, {"device": "nvidia"})
        self.assertIn("kv_transfer_config", test_params)
        config = json.loads(test_params.get("kv_transfer_config"))
        self.assertEqual(config["kv_connector"], "NixlConnector")
        self.assertEqual(config["kv_role"], "kv_both")


if __name__ == "__main__":
    unittest.main()