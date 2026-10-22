"""2026-10-23 交接前全系統檢查抓到的問題的回歸測試。"""
import pytest

from guardian.yaml_validator import check_image


@pytest.mark.parametrize("image", ["nginx", "nginx:1.28", "bitnami/redis:7.2", "registry.example.com/team/app:v1",
                                   "localhost:5000/my-app:latest", "ghcr.io/org/app@sha256:" + "a" * 64])
def test_valid_image_names_pass(image):
    assert check_image(image) == []


@pytest.mark.parametrize("image", ["檔換成", "Nginx:1", "nginx latest", "", "nginx:", "-nginx"])
def test_invalid_image_names_blocked(image):
    """不合法的名稱送進叢集只會 InvalidImageName，新 Pod 永遠起不來，要在平台這端就擋下並說明。"""
    errs = check_image(image)
    assert errs and "不是合法的映像檔名稱" in errs[0]


@pytest.mark.parametrize("text", ["把 final-test 的映像檔換成 nginx:1.28", "把 final-test 的映像換成 nginx:1.28",
                                  "final-test 的鏡像檔改成 nginx:1.28", "把 final-test 的 image 更新成 nginx:1.28"])
def test_update_image_rule_reads_image_not_filler(text):
    """「映像檔換成」的「檔換成」曾被當成映像檔名稱。"""
    web_demo = pytest.importorskip("web_demo")
    hit = web_demo._rule_intent(text)
    assert hit["action"] == "update_image" and hit["args"] == {"name": "final-test", "image": "nginx:1.28"}
