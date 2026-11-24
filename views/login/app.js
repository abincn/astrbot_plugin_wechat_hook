const $ = (id) => document.getElementById(id);
const bridge = window.AstrBotPluginView;

const state = {
  ready: false,
  busy: false,
  platformId: "",
  lastQrHash: "",
  timer: null,
};

const LABELS = {
  logged_in: "已在线",
  qr_code: "等待扫码",
  auto_login_button: "等待点击登录",
  maintenance: "计划维护中",
  offline: "Hook 离线",
  auth_error: "Token 鉴权失败",
  unsupported: "Hook 版本过旧",
  busy: "采集中",
  unavailable: "未就绪",
  invalid_config: "配置错误",
  error: "接口异常",
};

function setNotice(text, isError = false) {
  $("notice").textContent = text;
  $("notice").classList.toggle("error", isError);
}

function setBusy(busy) {
  state.busy = busy;
  $("refresh-btn").disabled = busy || !state.ready;
  $("platform-select").disabled = busy || !state.ready;
  document.querySelector("main").setAttribute("aria-busy", String(busy));
}

function scheduleNext(status) {
  if (state.timer) {
    clearTimeout(state.timer);
    state.timer = null;
  }
  if (!state.ready || !$("auto-refresh").checked) return;
  const delayMs = ["qr_code", "auto_login_button", "busy", "unavailable"].includes(status) ? 3000 : 10000;
  state.timer = setTimeout(() => {
    refreshStatus(false);
  }, delayMs);
}

function renderStatus(data) {
  const status = String(data?.status || "error");
  const pill = $("status-pill");
  pill.textContent = LABELS[status] || status;
  pill.className = `pill ${status}`;

  $("meta-id").textContent = data?.platform_id || "—";
  $("meta-wxid").textContent = data?.self_wxid || "（未填 self_wxid）";
  $("meta-origin").textContent = data?.hook_origin || "—";
  $("meta-state").textContent = LABELS[status] || status;

  if (status === "qr_code" && data?.qr_data_url) {
    $("qr-panel").hidden = false;
    $("empty-panel").hidden = true;
    const nextHash = String(data.qr_sha256 || "");
    if (!nextHash || nextHash !== state.lastQrHash) {
      $("qr-img").src = data.qr_data_url;
      state.lastQrHash = nextHash;
    }
    $("qr-meta").textContent = nextHash
      ? `指纹：${nextHash} · 刷新时间：${new Date().toLocaleTimeString("zh-CN", { hour12: false })}`
      : "";
    setNotice(data.detail || "请使用手机微信扫码登录。");
  } else {
    $("qr-panel").hidden = true;
    $("empty-panel").hidden = false;
    if (state.lastQrHash) {
      $("qr-img").removeAttribute("src");
      state.lastQrHash = "";
    }
    const icons = {
      logged_in: "✓",
      maintenance: "☾",
      auto_login_button: "▶",
      offline: "✕",
      auth_error: "!",
    };
    $("empty-icon").textContent = icons[status] || "◎";
    $("empty-title").textContent = LABELS[status] || "状态更新";
    $("empty-desc").textContent = data?.detail || "暂无详细信息。";
    setNotice(data?.detail || "状态已更新。", ["offline", "error", "auth_error", "invalid_config"].includes(status));
  }
  scheduleNext(status);
}

async function loadPlatforms() {
  const resp = await bridge.apiPost("login", { operation: "list" });
  const list = Array.isArray(resp?.platforms) ? resp.platforms : [];
  const select = $("platform-select");
  select.replaceChildren();
  if (!list.length) {
    const opt = document.createElement("option");
    opt.value = "";
    opt.textContent = "未找到 wechat_pc_hook 平台";
    select.append(opt);
    state.platformId = "";
    return false;
  }
  for (const item of list) {
    const opt = document.createElement("option");
    opt.value = item.id;
    const suffix = item.enable ? "" : "（未启用）";
    opt.textContent = `${item.id} · ${item.self_wxid || "未设wxid"}${suffix}`;
    select.append(opt);
  }
  if (!state.platformId || !list.some((x) => x.id === state.platformId)) {
    const preferred = list.find((x) => x.enable) || list[0];
    state.platformId = preferred.id;
  }
  select.value = state.platformId;
  return true;
}

async function refreshStatus(manual = true) {
  if (!state.ready || state.busy) return;
  setBusy(true);
  try {
    if (manual || !state.platformId) {
      const ok = await loadPlatforms();
      if (!ok) {
        $("qr-panel").hidden = true;
        $("empty-panel").hidden = false;
        $("empty-title").textContent = "未配置微信 Hook 平台";
        $("empty-desc").textContent = "请先在 AstrBot「平台 -> 添加平台」中创建并配置 微信 Hook 连接。";
        $("status-pill").textContent = "未配置";
        $("status-pill").className = "pill subtle-pill";
        setNotice("尚未检测到 wechat_pc_hook 平台配置。", true);
        return;
      }
    }
    const data = await bridge.apiPost("login", {
      operation: "status",
      platform_id: state.platformId,
      include_qr: true,
    });
    renderStatus(data);
  } catch (error) {
    setNotice(error?.message || "请求失败，请检查 AstrBot 后端状态。", true);
    scheduleNext("error");
  } finally {
    setBusy(false);
  }
}

$("refresh-btn").addEventListener("click", () => refreshStatus(true));
$("platform-select").addEventListener("change", () => {
  state.platformId = $("platform-select").value;
  state.lastQrHash = "";
  refreshStatus(true);
});
$("auto-refresh").addEventListener("change", () => {
  if ($("auto-refresh").checked) {
    refreshStatus(false);
  } else if (state.timer) {
    clearTimeout(state.timer);
    state.timer = null;
  }
});

if (!bridge) {
  setNotice("请在 AstrBot 后台「插件 -> 微信 Hook 连接 -> 可视化视图」中打开本页面。", true);
  setBusy(false);
} else {
  bridge.ready()
    .then(() => {
      state.ready = true;
      setBusy(false);
      return refreshStatus(true);
    })
    .catch(() => setNotice("AstrBot 页面接口初始化失败，请刷新页面重试。", true));
}
