"""
測試 guardian/yaml_validator.py：dry-run 之前的第一道靜態檢查關卡。
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from guardian.yaml_validator import validate_yaml

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
