"""图片水印动作的测试。

重点是那几个**容易出错但不容易发现**的行为：
  * EXIF 方向必须在水印之前应用（否则手机照片的水印会横竖颠倒）
  * EXIF 方向标记必须在输出里清掉（否则看图软件会二次旋转）
  * 平铺不能留下空白边角
  * JPEG 输出必须拍平透明通道（否则报错或出现黑块）
  * 重名要按 Windows 习惯变成 "名字 (2).jpg"
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from docforge.actions import ActionContext, ActionError, get_action
from docforge.actions.base import resolve_output_path

SPEC = get_action("image.watermark")


def _make_image(path: Path, size: tuple[int, int] = (400, 300), color: str = "#2A6FB0") -> Path:
    Image.new("RGB", size, color).save(path)
    return path


def _run(tmp_path: Path, src: Path, params: dict, *, name: str = "out.png") -> Path:
    out = tmp_path / name
    ctx = ActionContext(
        job_id="j1",
        task_id="t1",
        file_path=str(src),
        output_path=str(out),
        params=params,
    )
    result = SPEC.handler(ctx)
    assert result.output_path
    return Path(result.output_path)


# --------------------------------------------------------------------------- #
# 基础行为                                                                      #
# --------------------------------------------------------------------------- #

def test_text_watermark_produces_different_image(tmp_path: Path) -> None:
    src = _make_image(tmp_path / "a.png")
    out = _run(tmp_path, src, {"mode": "text", "text": "机密", "position": "center"})

    assert out.is_file()
    assert out.stat().st_size > 0

    with Image.open(src) as original, Image.open(out) as produced:
        # 尺寸必须保持一致 —— 加水印不该改变画布
        assert produced.size == original.size
        assert list(produced.convert("RGB").getdata()) != list(original.convert("RGB").getdata())


def test_empty_text_is_rejected(tmp_path: Path) -> None:
    src = _make_image(tmp_path / "a.png")
    with pytest.raises(ActionError, match="水印文字为空"):
        _run(tmp_path, src, {"mode": "text", "text": "   "})


def test_image_mode_requires_logo_path(tmp_path: Path) -> None:
    src = _make_image(tmp_path / "a.png")
    with pytest.raises(ActionError, match="缺少水印图片路径"):
        _run(tmp_path, src, {"mode": "image", "image_path": ""})


def test_missing_logo_reports_readable_error(tmp_path: Path) -> None:
    src = _make_image(tmp_path / "a.png")
    with pytest.raises(ActionError, match="水印图片不存在"):
        _run(tmp_path, src, {"mode": "image", "image_path": str(tmp_path / "nope.png")})


def test_logo_watermark_works(tmp_path: Path) -> None:
    src = _make_image(tmp_path / "a.png")
    logo = Image.new("RGBA", (80, 40), (255, 0, 0, 255))
    logo_path = tmp_path / "logo.png"
    logo.save(logo_path)

    out = _run(tmp_path, src, {"mode": "image", "image_path": str(logo_path), "position": "top-left"})
    assert out.is_file() and out.stat().st_size > 0


# --------------------------------------------------------------------------- #
# 位置与平铺                                                                    #
# --------------------------------------------------------------------------- #

def test_positions_differ(tmp_path: Path) -> None:
    src = _make_image(tmp_path / "a.png")
    left = _run(tmp_path, src, {"mode": "text", "text": "ACME", "position": "top-left"}, name="l.png")
    right = _run(tmp_path, src, {"mode": "text", "text": "ACME", "position": "bottom-right"}, name="r.png")

    with Image.open(left) as a, Image.open(right) as b:
        assert list(a.convert("RGB").getdata()) != list(b.convert("RGB").getdata())


def test_tile_mode_marks_all_four_corners(tmp_path: Path) -> None:
    """平铺最容易出的问题是四角留白 —— 这里直接检查四个角区域都被改动过。"""
    src = _make_image(tmp_path / "a.png", size=(900, 700), color="#F0F0F0")
    out = _run(
        tmp_path,
        src,
        {
            "mode": "text",
            "text": "CONFIDENTIAL",
            "position": "tile",
            "opacity": 0.9,
            "rotation": 0,
            "font_size_ratio": 0.06,
            "tile_gap_ratio": 0.2,
        },
    )

    with Image.open(src) as original, Image.open(out) as produced:
        o = original.convert("RGB")
        p = produced.convert("RGB")
        w, h = o.size
        corners = [
            (2, 2, w // 5, h // 5),
            (w - w // 5, 2, w - 2, h // 5),
            (2, h - h // 5, w // 5, h - 2),
            (w - w // 5, h - h // 5, w - 2, h - 2),
        ]
        for box in corners:
            assert list(o.crop(box).getdata()) != list(p.crop(box).getdata()), f"角落 {box} 没有被平铺覆盖"


# --------------------------------------------------------------------------- #
# EXIF 方向                                                                     #
# --------------------------------------------------------------------------- #

def test_exif_orientation_applied_before_watermark(tmp_path: Path) -> None:
    """手机竖拍照片带 Orientation=6，宽高需要在打水印前被交换。"""
    src = tmp_path / "rotated.jpg"
    image = Image.new("RGB", (400, 200), "#3050A0")
    exif = image.getexif()
    exif[0x0112] = 6  # Rotate 90 CW
    image.save(src, exif=exif.tobytes())

    out = _run(tmp_path, src, {"mode": "text", "text": "X", "auto_orient": True}, name="fixed.jpg")

    with Image.open(out) as produced:
        # 400x200 经 90° 旋转后应变成 200x400
        assert produced.size == (200, 400)


def test_auto_orient_can_be_disabled(tmp_path: Path) -> None:
    src = tmp_path / "rotated.jpg"
    image = Image.new("RGB", (400, 200), "#3050A0")
    exif = image.getexif()
    exif[0x0112] = 6
    image.save(src, exif=exif.tobytes())

    out = _run(tmp_path, src, {"mode": "text", "text": "X", "auto_orient": False}, name="raw.jpg")
    with Image.open(out) as produced:
        assert produced.size == (400, 200)


def test_orientation_tag_is_stripped_from_output(tmp_path: Path) -> None:
    """转置已把方向烘焙进像素，标记必须清除，否则看图软件会再转一次。"""
    src = tmp_path / "rotated.jpg"
    image = Image.new("RGB", (400, 200), "#3050A0")
    exif = image.getexif()
    exif[0x0112] = 6
    image.save(src, exif=exif.tobytes())

    out = _run(tmp_path, src, {"mode": "text", "text": "X", "keep_exif": True}, name="clean.jpg")
    with Image.open(out) as produced:
        assert produced.getexif().get(0x0112) in (None, 1)


# --------------------------------------------------------------------------- #
# 输出格式                                                                      #
# --------------------------------------------------------------------------- #

def test_jpeg_output_from_transparent_source(tmp_path: Path) -> None:
    """PNG(RGBA) → JPEG 必须拍平透明通道，否则 PIL 会直接抛错。

    这里显式指定 output_format=jpg：这正是当初发现的 bug 场景 ——
    若不按**最终格式**决定是否拍平，就会拿 RGBA 去写 JPEG 而崩溃。
    """
    src = tmp_path / "alpha.png"
    Image.new("RGBA", (320, 240), (10, 200, 120, 90)).save(src)

    out = _run(
        tmp_path,
        src,
        {"mode": "text", "text": "测试", "quality": 80, "output_format": "jpg"},
        name="flat.jpg",
    )
    assert out.suffix == ".jpg"
    with Image.open(out) as produced:
        assert produced.format == "JPEG"
        assert produced.mode == "RGB"
        assert produced.size == (320, 240)


def test_output_format_same_follows_source_extension(tmp_path: Path) -> None:
    """output_format='same' 时必须跟随源格式，不能因为输出路径后缀而漂移。"""
    src = _make_image(tmp_path / "keep.png")

    out = _run(tmp_path, src, {"mode": "text", "text": "X", "output_format": "same"}, name="keep.png")
    assert out.suffix == ".png"
    with Image.open(out) as produced:
        assert produced.format == "PNG"


def test_webp_output_format(tmp_path: Path) -> None:
    src = _make_image(tmp_path / "a.png")
    out = _run(tmp_path, src, {"mode": "text", "text": "X", "output_format": "webp"}, name="a.webp")
    assert out.suffix == ".webp"
    with Image.open(out) as produced:
        assert produced.format == "WEBP"


def test_template_variables_are_expanded(tmp_path: Path) -> None:
    """每张唯一水印：模板变量必须真的被替换，且未知变量保持原样不报错。"""
    src = _make_image(tmp_path / "secret-doc.png")
    ctx = ActionContext(
        job_id="j",
        task_id="t",
        file_path=str(src),
        output_path=str(tmp_path / "o.png"),
        params={"mode": "text", "text": "{stem}", "unique_per_file": True},
    )
    # 直接验证展开函数，避免依赖像素比对
    from docforge.actions.image_watermark import _expand_template

    assert _expand_template("{stem}", ctx, 1) == "secret-doc"
    assert _expand_template("{index}", ctx, 7) == "7"
    assert _expand_template("{unknown}", ctx, 1) == "{unknown}"


# --------------------------------------------------------------------------- #
# 输出路径                                                                      #
# --------------------------------------------------------------------------- #

def test_conflict_rename_uses_windows_convention(tmp_path: Path) -> None:
    src = _make_image(tmp_path / "photo.png")
    out_dir = tmp_path / "out"

    first = resolve_output_path(str(src), str(out_dir), suffix="_wm", policy="rename")
    assert first.name == "photo_wm.png"
    first.write_bytes(b"x")

    second = resolve_output_path(str(src), str(out_dir), suffix="_wm", policy="rename")
    assert second.name == "photo_wm (2).png"


def test_conflict_skip_keeps_existing(tmp_path: Path) -> None:
    src = _make_image(tmp_path / "photo.png")
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    target = resolve_output_path(str(src), str(out_dir), policy="skip")
    target.write_bytes(b"existing")
    again = resolve_output_path(str(src), str(out_dir), policy="skip")
    assert again == target


def test_preserve_tree_keeps_subdirectories(tmp_path: Path) -> None:
    root = tmp_path / "root"
    nested = root / "a" / "b"
    nested.mkdir(parents=True)
    src = nested / "deep.png"
    Image.new("RGB", (10, 10)).save(src)

    out = resolve_output_path(
        str(src), str(tmp_path / "out"), policy="rename", preserve_tree=True, root=str(root)
    )
    assert out.parent == tmp_path / "out" / "a" / "b"


# --------------------------------------------------------------------------- #
# HEIC / HEIF（iPhone 照片）                                                    #
# --------------------------------------------------------------------------- #

def test_heif_support_is_registered() -> None:
    """只装 pillow-heif 是不够的，必须注册打开器才能读 HEIC。

    这是"承诺了却做不到"的典型：图片水印与 OCR 的 accepts 里都写着 heic，
    但没注册的话用户拖进 iPhone 照片只会看到"无法打开图片"。
    """
    from docforge.imaging import ensure_heif_support, heif_available

    if not heif_available():
        pytest.skip("未安装 pillow-heif")

    assert ensure_heif_support() is True
    # 注册应当对 Pillow 全局生效
    assert ".heic" in Image.registered_extensions()


def test_action_declares_heic_support() -> None:
    """**能力清单必须与实际行为一致。**

    动作的 ``accepts`` 是任务队列与界面共同的事实来源：
    队列据此决定"这个文件要不要处理"，界面据此决定"要不要把它列出来"。
    少声明一个格式，用户拖进来的 iPhone 照片会被直接跳过，
    而任务状态仍然是 succeeded —— 界面上什么都看不出来，只是文件没产出。
    """
    for ext in ("heic", "heif", "avif"):
        assert ext in SPEC.accepts, f"动作未声明支持 .{ext}"
        assert SPEC.accepts_file(f"photo.{ext}"), f".{ext} 应被接受"

    # 同时不能把无关类型也放进来
    assert not SPEC.accepts_file("document.pdf")
    assert not SPEC.accepts_file("sheet.xlsx")


def test_watermark_on_heic_input(tmp_path: Path) -> None:
    """真拿一张 HEIC 走完整水印流程，确认端到端可用。"""
    pillow_heif = pytest.importorskip("pillow_heif")
    from docforge.imaging import ensure_heif_support

    ensure_heif_support()

    source = tmp_path / "iphone.heic"
    Image.new("RGB", (800, 600), "#2A6FB0").save(source, format="HEIF")

    out = _run(
        tmp_path,
        source,
        {"mode": "text", "text": "HEIC 测试", "position": "center"},
        name="iphone_out.png",
    )

    assert out.is_file() and out.stat().st_size > 0
    with Image.open(out) as produced:
        assert produced.size == (800, 600)
