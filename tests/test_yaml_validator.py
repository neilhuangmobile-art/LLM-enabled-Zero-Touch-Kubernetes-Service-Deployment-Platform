"""
測試 guardian/yaml_validator.py：dry-run 之前的第一道靜態檢查關卡。
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from guardian.yaml_validator import validate_yaml, check_image, _check_images, _check_naming

GOOD_YAML = """
apiVersion: apps/v1
kind: Deployment
metadata:
  name: web-frontend
spec:
  replicas: 3
  selector:
    matchLabels:
      app: web-frontend
  template:
    metadata:
      labels:
        app: web-frontend
    spec:
      containers:
      - name: web-frontend
        image: nginx:1.25-alpine
        resources:
          limits:
            memory: 256Mi
"""

BAD_YAML = """
apiVersion: apps/v1
kind: Deployment
metadata:
  name: bad-app
spec:
  replicas: 200
  selector:
    matchLabels:
      app: bad-app
  template:
    metadata:
      labels:
        app: bad-app
    spec:
      hostNetwork: true
      containers:
      - name: bad-app
        image: alpine:latest
        securityContext:
          privileged: true
"""


class TestValidateYaml:
    def test_good_manifest_passes(self):
        result = validate_yaml(GOOD_YAML)
        assert result["ok"] is True
        assert result["errors"] == []

    def test_privileged_and_host_network_are_blocking_errors(self):
        result = validate_yaml(BAD_YAML)
        assert result["ok"] is False
        joined = " ".join(result["errors"])
        assert "privileged" in joined
        assert "hostNetwork" in joined

    def test_replicas_over_policy_max_is_blocking_error(self):
        result = validate_yaml(BAD_YAML)
        assert any("replicas=200" in e for e in result["errors"])

    def test_missing_required_field_is_error(self):
        result = validate_yaml({"kind": "Deployment"})  # 缺 apiVersion、metadata
        assert result["ok"] is False
        assert any("apiVersion" in e for e in result["errors"])

    def test_malformed_yaml_string_does_not_raise(self):
        result = validate_yaml(": : : not valid [[[")
        assert result["ok"] is False
        assert result["errors"] != []

    def test_dict_input_and_string_input_agree(self):
        import yaml
        as_dict = yaml.safe_load(GOOD_YAML)
        result_dict = validate_yaml(as_dict)
        result_str = validate_yaml(GOOD_YAML)
        assert result_dict["ok"] == result_str["ok"] is True


class TestNamingAndImagePolicy:
    """
    2026-09-14 補上：policy_rules.yaml 的 naming（denied_names/max_name_length）跟
    images（denied_images/allowed_registries）區塊在 validate_yaml() 裡從沒被讀取過，
    是完全沒被強制執行的死設定——這裡的測試針對新補上的 _check_naming/_check_images。
    """

    def test_denied_name_is_blocked(self):
        manifest = {
            "apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"name": "kube-system"},
            "spec": {"replicas": 1, "template": {"spec": {"containers": [
                {"name": "app", "image": "nginx:1.25"},
            ]}}},
        }
        result = validate_yaml(manifest)
        assert result["ok"] is False
        assert any("kube-system" in e for e in result["errors"])

    def test_name_exceeding_max_length_is_blocked(self):
        long_name = "a" * 64  # policy 上限 63
        manifest = {
            "apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"name": long_name},
            "spec": {"replicas": 1, "template": {"spec": {"containers": [
                {"name": "app", "image": "nginx:1.25"},
            ]}}},
        }
        result = validate_yaml(manifest)
        assert result["ok"] is False
        assert any("超過上限" in e for e in result["errors"])

    def test_denied_image_is_blocked_when_policy_configures_it(self):
        manifest = {
            "apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"name": "web-frontend"},
            "spec": {"replicas": 1, "template": {"spec": {"containers": [
                {"name": "app", "image": "alpine:latest"},
            ]}}},
        }
        custom_policy = {"images": {"denied_images": ["alpine:latest"]}}
        errors, _ = _check_images(manifest, custom_policy)
        assert any("alpine:latest" in e for e in errors)

    def test_empty_policy_lists_do_not_block_anything(self):
        # policy_rules.yaml 預設 denied_images/allowed_registries 都是空的，
        # 空清單要代表「不限制」，不能讓現有的正常部署被誤擋。
        result = validate_yaml(GOOD_YAML)
        assert result["ok"] is True

    def test_crypto_miner_images_are_blocked_by_default_policy(self):
        """2026-10-09 實測：metal3d/xmrig 原本只得到「沒指定版本」警告就通過。
        預設 policy_rules.yaml 的 denied_image_keywords 要擋下來，不分大小寫、
        關鍵字出現在 registry、路徑或 tag 都算。"""
        for image in ["metal3d/xmrig:latest", "ghcr.io/foo/XMRig:6.21",
                      "docker.io/someone/tools:cpuminer-opt", "nicehash/excavator"]:
            assert check_image(image), image
        manifest = {
            "apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"name": "web-frontend"},
            "spec": {"replicas": 1, "template": {"spec": {"containers": [
                {"name": "app", "image": "metal3d/xmrig:6.21",
                 "resources": {"limits": {"memory": "128Mi", "cpu": "100m"}}},
            ]}}},
        }
        result = validate_yaml(manifest)
        assert result["ok"] is False
        assert any("挖礦" in e for e in result["errors"])

    def test_common_images_are_not_mistaken_for_miners(self):
        for image in ["nginx:1.27", "redis:7", "postgres:16", "mongo:7",
                      "grafana/grafana:11.0.0", "minio/minio:latest", "busybox:1.36"]:
            assert check_image(image) == [], image

    def test_allowed_registries_blocks_images_outside_allowlist(self):
        manifest = {
            "apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"name": "web-frontend"},
            "spec": {"replicas": 1, "template": {"spec": {"containers": [
                {"name": "app", "image": "some-untrusted-registry.example/app:1.0"},
            ]}}},
        }
        custom_policy = {"images": {"allowed_registries": ["docker.io/library/"]}}
        errors, _ = _check_images(manifest, custom_policy)
        assert any("不在允許的倉庫清單內" in e for e in errors)

    def test_digit_start_name_is_blocked(self):
        """2026-10-03 實測發現：Deployment 名稱本身允許數字開頭（DNS-1123），但這個
        系統一定會另外建一個 "<name>-svc" 的 Service，Service 名稱用更嚴格的
        DNS-1035（一定要字母開頭）——純數字名稱（例如「123」）會在最後真的呼叫
        K8s API 才失敗，不應該讓它通過這裡的靜態檢查。"""
        manifest = {
            "apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"name": "123"},
            "spec": {"replicas": 1, "template": {"spec": {"containers": [
                {"name": "app", "image": "nginx:1.25"},
            ]}}},
        }
        result = validate_yaml(manifest)
        assert result["ok"] is False
        assert any("必須以小寫英文字母開頭" in e for e in result["errors"])

    def test_name_ending_with_hyphen_is_blocked(self):
        manifest = {
            "apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"name": "app-"},
            "spec": {"replicas": 1, "template": {"spec": {"containers": [
                {"name": "app", "image": "nginx:1.25"},
            ]}}},
        }
        result = validate_yaml(manifest)
        assert result["ok"] is False
        assert any("必須以小寫英文字母開頭" in e for e in result["errors"])

    def test_letter_start_name_with_digits_is_allowed(self):
        """字母開頭、中間含數字跟連字號的名稱（例如「app-123」）應該正常通過，
        不能因為新加的格式檢查誤擋合法名稱。"""
        manifest = {
            "apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"name": "app-123"},
            "spec": {"replicas": 1, "template": {"spec": {"containers": [
                {"name": "app", "image": "nginx:1.25"},
            ]}}},
        }
        result = validate_yaml(manifest)
        assert result["ok"] is True
