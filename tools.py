import json
import re
import time
from xml.etree import ElementTree as ET

_BOUNDS_RE = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")

# 预编译正则，提升性能
PATTERN_CURRENT_FOCUS = re.compile(r'mCurrentFocus=Window\{.*?\s+(\S+)')

def parse_mcurrent_focus(raw_text: str) -> dict:
    """
    仅解析 dumpsys window 原始输出字符串，提取当前焦点窗口信息
    :param raw_text: adb shell dumpsys window 返回完整文本 或 单行mCurrentFocus行
    :return:
        {
            "full_component": str|None,    # 原始截取字符串
            "package": str|None,
            "activity": str|None,
            "is_valid_app": bool,           # 是否有效应用组件（区分通知栏/锁屏等系统窗口）
        }
    """
    result = {
        "full_component": None,
        "package": None,
        "activity": None,
        "is_valid_app": False
    }

    match = PATTERN_CURRENT_FOCUS.search(raw_text)
    if not match:
        return result

    component_str = match.group(1).strip()
    result["full_component"] = component_str

    # 区分两种格式：
    # 1. com.nothing.launcher/com.android.searchlauncher.SearchLauncher
    # 2. NotificationShade（无斜杠，系统弹窗/通知栏）
    if "/" in component_str:
        pkg, act = component_str.split("/", 1)
        result["package"] = pkg
        result["activity"] = act
        # 简单判定有效应用：包名包含小数点
        if "." in pkg:
            result["is_valid_app"] = True

    return result


def _parse_bounds(bounds_str: str):
    m = _BOUNDS_RE.match(bounds_str)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))


def _is_interactive(node) -> bool:
    """是否为可交互控件（可作为组合控件父级）：clickable/checkable/long-clickable 任一为 true。"""
    return (node.get("clickable") == "true"
            or node.get("checkable") == "true"
            or node.get("long-clickable") == "true")


def _bounds_contains(outer, inner) -> bool:
    """inner 是否完全位于 outer 内部（非严格，允许相等）。入参为 (x1,y1,x2,y2)。"""
    return (outer[0] <= inner[0] and outer[1] <= inner[1]
            and outer[2] >= inner[2] and outer[3] >= inner[3])


def _is_meaningful(node) -> bool:
    if node.get("class", "") == "android.widget.FrameLayout" or node.get("class", "") == "android.widget.LinearLayout":
        return False
    if node.get("resource-id", "").strip():
        return True
    if node.get("text", "").strip():
        return True
    if node.get("content-desc", "").strip():
        return True
    if node.get("clickable") == "true":
        return True
    if node.get("scrollable") == "true":
        return True
    if node.get("checkable") == "true":
        return True
    return False


# 属性优先级（来自 test3.py）：决定定位器累加顺序
ATTR_PRIORITY = [
    "resource-id", "text", "content-desc", "class",
    "index", "clickable", "enabled", "focusable", "package",
]

# XML 属性名 → uiautomator2 selector 键
ATTR_MAP = {
    "resource-id": "resourceId",
    "text": "text",
    "content-desc": "description",
    "class": "className",
    "package": "packageName",
    "long-clickable": "longClickable",
    "checkable": "checkable",
    "checked": "checked",
    "clickable": "clickable",
    "scrollable": "scrollable",
    "enabled": "enabled",
    "focusable": "focusable",
    "focused": "focused",
    "selected": "selected",
}


def _attrs_from_node(node) -> dict:
    """从 XML node 提取属性并转换为 uiautomator2 selector 键名（保留完整 className 等）。"""
    attrs = {}
    for xml_k, out_k in ATTR_MAP.items():
        if xml_k in node.attrib:
            val = node.attrib[xml_k]
            if val in ("true", "false"):
                val = (val == "true")
            elif xml_k == "index" and val.isdigit():
                val = int(val)
            attrs[out_k] = val
    return attrs


def _build_locators(all_nodes_attrs: list) -> list:
    """为每个节点生成最小唯一定位器（移植自 test3.py parse_ui_hierarchy）。

    instance 在全节点列表上计算，避免过滤导致漂移。
    返回与输入等长的 locator 列表。
    """
    locators = []
    for i, attrs in enumerate(all_nodes_attrs):
        loc = {}
        is_unique = False
        for xml_attr in ATTR_PRIORITY:
            out_attr = ATTR_MAP.get(xml_attr, xml_attr)
            val = attrs.get(out_attr)
            # 跳过空值（空 text/desc、False 布尔不参与定位）
            if val is None or val == "" or val is False:
                continue
            loc[out_attr] = val
            # 数当前 loc 在全图所有节点中的匹配数
            test_matches = 0
            for a in all_nodes_attrs:
                match = True
                for k, v in loc.items():
                    if a.get(k) != v:
                        match = False
                        break
                if match:
                    test_matches += 1
            if test_matches == 1:
                is_unique = True
                break
        # 仍不唯一 → 计算 instance（当前 loc 筛选下，i 之前有几个匹配）
        if not is_unique:
            instance_idx = 0
            for j in range(i):
                a = all_nodes_attrs[j]
                match = True
                for k, v in loc.items():
                    if a.get(k) != v:
                        match = False
                        break
                if match:
                    instance_idx += 1
            loc["instance"] = instance_idx
        locators.append(loc)
    return locators


# 需要附加显示的布尔属性顺序（enabled 特殊处理，单独在 _build_display_tags 中处理）
_DISPLAY_ATTR_ORDER = [
    "long-clickable", "checkable", "checked", "clickable",
    "scrollable", "focused", "selected",
]


def _build_display_tags(node) -> list:
    """构建附加显示标签列表：
    - 上述 8 个属性值为 true 时附加属性名（kebab-case 原样）
    - enabled 为 false 时附加 'disabled'，为 true 时不附加
    """
    tags = []
    for attr in _DISPLAY_ATTR_ORDER:
        if node.get(attr) == "true":
            tags.append(attr)
    if node.get("enabled") == "false":
        tags.append("disabled")
    return tags


def _collect_meaningful_nodes(root):
    # 先按深度优先顺序收集全部 node 及其转换属性
    all_nodes = list(root.iter("node"))
    all_attrs = [_attrs_from_node(n) for n in all_nodes]
    locators = _build_locators(all_attrs)

    # XML 父节点映射，用于向上查找最近的"可交互且有意义"祖先
    parent_map = {c: p for p in root.iter() for c in p}

    result = []
    meaningful_xml = []   # 与 result 对齐的 XML 节点引用
    xml_to_idx = {}       # XML 节点 -> result 位置索引
    for node, loc in zip(all_nodes, locators):
        if not _is_meaningful(node):
            continue
        bounds_str = node.get("bounds", "")
        class_name = node.get("class", "")
        if "." in class_name:
            class_name = class_name.rsplit(".", 1)[-1]
        bounds = _parse_bounds(bounds_str)
        item = {
            "resource-id": node.get("resource-id", "").strip(),
            "text": node.get("text", "").strip(),
            "content-desc": node.get("content-desc", "").strip(),
            "class": class_name,
            "bounds": bounds_str,
            "local": loc,
            "extra_tags": _build_display_tags(node),
            "group_parent": None,        # 组合控件父级索引（int）或 None
            "merged_idents": [],         # 单子合并时从子节点合并来的标识文本列表
        }
        if bounds:
            item["_center"] = ((bounds[0] + bounds[2]) // 2, (bounds[1] + bounds[3]) // 2)
            item["_bounds_tuple"] = bounds
        else:
            item["_bounds_tuple"] = None
        xml_to_idx[node] = len(result)
        result.append(item)
        meaningful_xml.append(node)

    # 计算分组：每个节点挂到最近的"有意义且 bounds 包含本节点"的 XML 祖先下
    # 支持多级嵌套；不限制父/子的交互性
    for i, node in enumerate(meaningful_xml):
        my_bounds = result[i]["_bounds_tuple"]
        if not my_bounds:
            continue
        anc = parent_map.get(node)
        while anc is not None:
            if anc in xml_to_idx:
                anc_item = result[xml_to_idx[anc]]
                anc_bounds = anc_item["_bounds_tuple"]
                if anc_bounds and _bounds_contains(anc_bounds, my_bounds):
                    result[i]["group_parent"] = xml_to_idx[anc]
                    break
            anc = parent_map.get(anc)

    # 按父级聚合子节点，用于单子节点合并判断
    children_of = {}
    for i, item in enumerate(result):
        p = item["group_parent"]
        if p is not None:
            children_of.setdefault(p, []).append(i)

    # 单子节点合并：仅当父可交互 + 唯一子非交互时；被合并节点的子节点提升到父级
    merge_children = set()
    merge_parent = {}   # 被合并子 -> 合并到的父级索引
    for p, kids in children_of.items():
        if len(kids) == 1:
            k = kids[0]
            if _is_interactive(meaningful_xml[p]) and not _is_interactive(meaningful_xml[k]):
                merge_children.add(k)
                merge_parent[k] = p

    if merge_children:
        # 合并前，把子节点的非空 text/content-desc/resource-id 追加到父级 merged_idents
        for k, p in merge_parent.items():
            child = result[k]
            for key in ("text", "content-desc", "resource-id"):
                if child.get(key):
                    result[p]["merged_idents"].append(child[key])
        # 被合并节点的子节点提升到合并父级
        for item in result:
            if item["group_parent"] in merge_children:
                item["group_parent"] = merge_parent[item["group_parent"]]
        # 移除被合并节点，重映射索引
        old_to_new = {}
        new_result = []
        for i, item in enumerate(result):
            if i in merge_children:
                continue
            old_to_new[i] = len(new_result)
            new_result.append(item)
        for item in new_result:
            if item["group_parent"] is not None:
                item["group_parent"] = old_to_new.get(item["group_parent"])
        result = new_result

    return result


def _format_nodes(nodes, show_attrs: bool = True) -> str:
    # 预计算每组子节点列表，用于递归渲染
    children_of = {}
    for i, item in enumerate(nodes):
        p = item.get("group_parent")
        if p is not None:
            children_of.setdefault(p, []).append(i)

    def build_core(i):
        item = nodes[i]
        ident_parts = [s for s in (item["text"], item["content-desc"], item["resource-id"])
                       if s]
        ident_parts.extend(item.get("merged_idents", []))
        ident = "/".join(ident_parts)
        parts = [f"[{i}]"]
        if ident:
            parts.append(ident)
        parts.append(item["class"])
        parts.append(item["bounds"])
        if show_attrs:
            parts.extend(item.get("extra_tags", []))
        return " ".join(parts)

    def render(i, indent, is_last):
        connector = "└─" if is_last else "├─"
        lines = [indent + connector + build_core(i)]
        child_indent = indent + ("   " if is_last else "│  ")
        kids = children_of.get(i, [])
        for j, k in enumerate(kids):
            lines.extend(render(k, child_indent, j == len(kids) - 1))
        return lines

    lines = []
    for i, item in enumerate(nodes):
        if item.get("group_parent") is not None:
            continue  # 由父级递归渲染
        lines.append(build_core(i))  # 根节点无前缀
        kids = children_of.get(i, [])
        for j, k in enumerate(kids):
            lines.extend(render(k, "  ", j == len(kids) - 1))
    return "\n".join(lines)


def _fetch_nodes(core, root_in_active:bool = True):
    xml = core.u2.dump_hierarchy(compressed=True, root_in_active=root_in_active)
    root = ET.fromstring(xml.encode("utf-8"))
    return _collect_meaningful_nodes(root)


class DeviceTools:
    """设备控制工具集合，可作为 MCP 工具或 LLM tools 使用。

    用法：
        tools = DeviceTools(core)

        # 1. 注册为 MCP 工具
        from core_api.mcp.adapters import register_mcp_tools
        register_mcp_tools(mcp, tools)

        # 2. 转为 OpenAI function calling tools
        from core_api.mcp.adapters import to_openai_tools, dispatch_tool
        openai_tools = to_openai_tools(tools)
        # ... LLM 返回 tool_call(name, arguments) ...
        result = dispatch_tool(tools, name, arguments)

        # 3. 直接调用方法
        tools.click(100, 200)
    """

    # 工具方法名清单（供适配器反射，跳过非工具方法如 _get_nodes）
    _TOOL_NAMES = (
        "open_app", "stop_app", "click", "swipe", "press_key",
        "dump_hierarchy", "click_by_index", "type_text",
        "list_launcher_apps", "device_info", "screenshot",
    )

    def __init__(self, core):
        self.core = core
        self._nodes_cache: list = []

    def _get_nodes(self, root_in_active: bool = True, use_cache: bool = True) -> list:
        if use_cache and self._nodes_cache:
            return self._nodes_cache
        self._nodes_cache = _fetch_nodes(self.core, root_in_active)
        return self._nodes_cache

    def open_app(self, pkg: str) -> str:
        """根据包名打开应用 pkg:应用包名"""
        self.core.app.open(pkg)
        return f"opened {pkg}"

    def stop_app(self, pkg: str) -> str:
        """根据包名关闭/停止应用 pkg:应用包名"""
        self.core.app.force_stop_package(pkg)
        return f"stopped {pkg}"

    def click(self, x: int, y: int) -> str:
        """在屏幕坐标 (x, y) 处点击。"""
        self.core.u2.click(x, y)
        return f"clicked ({x},{y})"

    def swipe(self, fx: int, fy: int, tx: int, ty: int, duration: float = 0.5) -> str:
        """从坐标 (fx, fy) 滑动到 (tx, ty)，duration 为滑动时长（秒）。"""
        self.core.u2.swipe(fx, fy, tx, ty, duration)
        return f"swiped from ({fx},{fy}) to ({tx},{ty})"

    def press_key(self, key: str) -> str:
        """按键。支持的键名：home, back, recent, enter, delete, menu, search,
        up, down, left, right, center, volume_up, volume_down, volume_mute,
        camera, power。"""
        self.core.u2.press(key)
        return f"pressed {key}"

    def dump_hierarchy(self, root_in_active: bool = True, show_attrs: bool = True) -> str:
        """获取当前界面 UI 层级结构（精简格式）。
        过滤掉无意义的布局节点，只保留有 id/text/desc 或可点击/可滚动/可勾选的关键控件。
        每个控件带索引（[0] [1] ...），可配合 click_by_index / type_text 按索引操作控件。
        本次获取的控件列表会被缓存，后续 click_by_index / type_text 可直接使用缓存，无需重新获取界面。

        组合控件分组：任意有意义控件间，只要满足 XML 祖先关系 + bounds 包含关系，
        即建立父子层级（├─/└─ 连接符缩进展示，支持多级嵌套）。可交互控件也可作为
        另一控件的子节点（如列表项内的播放按钮）。
        当可交互父级仅含一个非交互描述性子节点时，直接合并为单行（子节点不再单独编索引），
        子节点的 text/desc/id 合并到父级标识（用 / 分隔）；
        其他情况（多子节点、可交互子节点）保留独立索引。
        每个控件的 text/content-desc/resource-id 若有值都保留，用 / 分隔作为标识。

        :param root_in_active  默认root_in_active=true,只获取当前活动窗口的UI层级结构。
            root_in_active=false时，获取所有窗口的UI层级结构，比如包含状态栏，导航栏，侧边栏，悬浮窗等
        :param show_attrs: 是否在每行末尾附加控件属性标签
            （long-clickable/checkable/checked/clickable/scrollable/focusable/focused/selected 为 true 时附加，
             enabled 为 false 时附加 disabled），默认 true
        输出示例（show_attrs=true，含多级层级；[0] 为单子节点合并，[2] 为多子节点组含可交互子节点）：
          当前前台应用：mCurrentFocus=Window{7cafa38 u0 com.nothing.launcher/com.android.searchlauncher.SearchLauncher}
          [0] Back View [12,137][138,263] clickable
          [1] 设置 TextView [43,273][211,370]
          [2] View [42,593][1038,801] long-clickable clickable
            ├─[3] 星期二，10:46 TextView [95,635][867,704]
            ├─[4] 7月28日 TextView [95,706][236,758]
            ├─[5] 0:11 TextView [281,710][368,753]
            └─[6] Pause ImageView [870,625][996,751] clickable
        """
        nodes = self._get_nodes(root_in_active=root_in_active, use_cache=False)
        if not nodes:
            return "no meaningful nodes found"
        current_app_str = self.core.adb.shell("dumpsys window | grep mCurrentFocus")
        return f"当前前台应用：{current_app_str}\n" + _format_nodes(nodes, show_attrs=show_attrs)

    def click_by_index(self, index: int, long_click: bool = False) -> str:
        """按索引点击控件。索引对应 dump_hierarchy 输出中的序号。

        :param index: 控件索引
        :param long_click: 长按
        """
        nodes = self._get_nodes()
        if index < 0 or index >= len(nodes):
            return f"index {index} out of range (0-{len(nodes) - 1})"
        item = nodes[index]
        center = item.get("_center")
        if not center:
            return f"node {index} has no valid bounds"
        desc = f"id={item['resource-id']}" if item["resource-id"] else f"text={item['text']}"
        if long_click:
            self.core.u2.long_click(center[0], center[1], duration=1)
            return f"long clicked index {index} ({desc}) at {center}"
        else:
            self.core.u2.click(center[0], center[1])
            return f"clicked index {index} ({desc}) at {center}"

    def type_text(self, text: str, index: int) -> str:
        """点击指定索引的输入控件使其获得焦点，然后输入文本。

        :param text: 要输入的文本内容
        :param index: 输入控件的索引，对应 dump_hierarchy 输出中的序号
        """
        nodes = self._get_nodes()
        if index < 0 or index >= len(nodes):
            return f"index {index} out of range (0-{len(nodes) - 1})"
        item = nodes[index]
        center = item.get("_center")
        if not center:
            return f"node {index} has no valid bounds"
        desc = f"id={item['resource-id']}" if item["resource-id"] else f"text={item['text']}"
        # EditText 且有定位器：直接用 selector 精准 set_text，提前返回避免重复输入
        if item.get('class', '') == 'EditText' and item.get('local'):
            local = item['local']
            try:
                self.core.u2(**local).set_text(text)
                return f"typed '{text}' into index {index} ({desc}) via locator, status=ok"
            except Exception as e:
                return f"typed '{text}' into index {index} ({desc}) via locator, status=failed: {e}"
        # 回退路径：点击中心获得焦点，再用 IME 输入
        self.core.u2.click(center[0], center[1])
        time.sleep(1)
        result = self.core.ime.input_text(text)
        time.sleep(1)
        status = "ok" if result else "failed"
        return f"typed '{text}' into index {index} ({desc}) via ime, status={status}"

    def list_launcher_apps(self, query: str = "") -> str:
        """获取所有有启动页的应用列表，支持名称和包名的模糊过滤。

        :param query: 模糊匹配的关键字（为空则返回全部）
        :return: 应用列表，每行一个应用，格式：名称=xx 包名=xx version=xx version_code=xx
        """
        apps = self.core.app.get_launcher_apps(query)
        lines = []
        for app in apps:
            name = app.get("name", "")
            pkg = app.get("packageName", "")
            version_name = app.get("versionName", "")
            version_code = app.get("versionCode", "")
            lines.append(f"名称={name} 包名={pkg} version={version_name} version_code={version_code}")
        return "\n".join(lines)

    def device_info(self) -> str:
        """获取详细的设备信息"""
        result = self.core.system.get_device_info()
        compact_json = json.dumps(result, separators=(",", ":"))
        return compact_json

    def screenshot(self) -> bytes:
        """截取当前屏幕，返回 JPEG 图像的原始 bytes。

        注意：此方法返回原始 bytes。MCP 适配器会包装为 Image 对象，
        LLM 适配器会编码为 base64 字符串。
        """
        return self.core.screen.get_screenshot(80)
