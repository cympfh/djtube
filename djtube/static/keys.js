export const FADER_STEP = 0.04;
export const FADER_STEP_LARGE = 0.12;
export const JOG_STEP = 1;
export const JOG_STEP_LARGE = 10;

export const BINDINGS = [
  { keys: ["/"], action: "focusSearch", label: "検索にフォーカス", group: "検索" },
  { keys: ["Enter"], action: "onEnter", label: "検索 / ロード", group: "検索" },
  { keys: ["Escape"], action: "blurSearch", label: "検索から抜ける", group: "検索" },
  { keys: ["ArrowUp", "k"], action: "moveSelection", args: [-1], label: "結果を上へ", group: "検索" },
  { keys: ["ArrowDown", "j"], action: "moveSelection", args: [1], label: "結果を下へ", group: "検索" },
  { keys: ["m"], action: "toggleMusicOnly", label: "音楽に限る", group: "検索" },
  { keys: ["a"], action: "loadSelected", args: ["A"], label: "デッキ A へロード", group: "デッキ" },
  { keys: ["b"], action: "loadSelected", args: ["B"], label: "デッキ B へロード", group: "デッキ" },
  { keys: ["t"], action: "toggleLoadTarget", label: "ロード先を切り替え", group: "デッキ" },
  { keys: ["q"], action: "togglePlay", args: ["A"], label: "デッキ A 再生/停止", group: "デッキ" },
  { keys: ["w"], action: "togglePlay", args: ["B"], label: "デッキ B 再生/停止", group: "デッキ" },
  { keys: ["z"], action: "cue", args: ["A"], label: "デッキ A キュー", group: "デッキ" },
  { keys: ["x"], action: "cue", args: ["B"], label: "デッキ B キュー", group: "デッキ" },
  { keys: ["z"], action: "setCue", args: ["A"], shift: true, label: "デッキ A のキュー位置", group: "デッキ" },
  { keys: ["x"], action: "setCue", args: ["B"], shift: true, label: "デッキ B のキュー位置", group: "デッキ" },
  { keys: ["["], action: "jog", args: ["A", -JOG_STEP], label: "デッキ A を戻す", group: "ジョグ" },
  { keys: ["]"], action: "jog", args: ["A", JOG_STEP], label: "デッキ A を進める", group: "ジョグ" },
  { keys: ["["], action: "jog", args: ["A", -JOG_STEP_LARGE], shift: true, label: "デッキ A を大きく戻す", group: "ジョグ" },
  { keys: ["]"], action: "jog", args: ["A", JOG_STEP_LARGE], shift: true, label: "デッキ A を大きく進める", group: "ジョグ" },
  { keys: [";"], action: "jog", args: ["B", -JOG_STEP], label: "デッキ B を戻す", group: "ジョグ" },
  { keys: ["'"], action: "jog", args: ["B", JOG_STEP], label: "デッキ B を進める", group: "ジョグ" },
  { keys: [";"], action: "jog", args: ["B", -JOG_STEP_LARGE], shift: true, label: "デッキ B を大きく戻す", group: "ジョグ" },
  { keys: ["'"], action: "jog", args: ["B", JOG_STEP_LARGE], shift: true, label: "デッキ B を大きく進める", group: "ジョグ" },
  {
    keys: ["ArrowLeft", ","],
    action: "nudgeCrossfader",
    args: [-FADER_STEP],
    label: "フェーダーを A へ",
    group: "フェーダー",
  },
  {
    keys: ["ArrowRight", "."],
    action: "nudgeCrossfader",
    args: [FADER_STEP],
    label: "フェーダーを B へ",
    group: "フェーダー",
  },
  {
    keys: ["ArrowLeft"],
    action: "nudgeCrossfader",
    args: [-FADER_STEP_LARGE],
    shift: true,
    label: "フェーダーを大きく A へ",
    group: "フェーダー",
  },
  {
    keys: ["ArrowRight"],
    action: "nudgeCrossfader",
    args: [FADER_STEP_LARGE],
    shift: true,
    label: "フェーダーを大きく B へ",
    group: "フェーダー",
  },
  { keys: ["Home"], action: "setCrossfader", args: [0], label: "フェーダーを A 端へ", group: "フェーダー" },
  { keys: ["End"], action: "setCrossfader", args: [1], label: "フェーダーを B 端へ", group: "フェーダー" },
];

const KEY_LABELS = {
  ArrowUp: "↑",
  ArrowDown: "↓",
  ArrowLeft: "←",
  ArrowRight: "→",
  Escape: "Esc",
  Enter: "Enter",
  Home: "Home",
  End: "End",
};

function prettyKey(key) {
  if (KEY_LABELS[key]) return KEY_LABELS[key];
  if (key.length === 1) return key === "/" ? "/" : key.toUpperCase();
  return key;
}

function bindingKeyText(binding) {
  return binding.keys
    .map((key) => {
      const label = prettyKey(key);
      return binding.shift ? `Shift+${label}` : label;
    })
    .join(" ");
}

export function legendGroups() {
  const order = [];
  const items = new Map();
  for (const binding of BINDINGS) {
    if (!binding.group) continue;
    if (!items.has(binding.group)) {
      items.set(binding.group, []);
      order.push(binding.group);
    }
    const text = bindingKeyText(binding);
    const group = items.get(binding.group);
    const existing = group.find((item) => item.label === binding.label);
    if (existing) existing.keys = `${existing.keys} ${text}`;
    else group.push({ label: binding.label, keys: text });
  }
  return order.map((name) => ({ name, items: items.get(name) }));
}

const SHIFT_ALIASES = {
  "[": "{",
  "]": "}",
  ";": ":",
  "'": '"',
};

function sameKey(bindingKey, event) {
  if (event.key === bindingKey) return true;
  if (event.shiftKey && SHIFT_ALIASES[bindingKey] === event.key) return true;
  return (
    bindingKey.length === 1 &&
    event.key.length === 1 &&
    bindingKey.toLowerCase() === event.key.toLowerCase()
  );
}

export function bindingFor(event) {
  if (event.metaKey || event.ctrlKey || event.altKey) return null;
  return (
    BINDINGS.find((binding) => !!binding.shift === !!event.shiftKey && binding.keys.some((key) => sameKey(key, event))) ||
    null
  );
}

export function isSearchTarget(target) {
  return !!target && target.id === "search-input";
}

export function isTypingTarget(target) {
  if (!target) return false;
  if (target.isContentEditable) return true;
  const tag = target.tagName;
  if (tag === "TEXTAREA" || tag === "SELECT") return true;
  if (tag === "INPUT") {
    const type = (target.type || "text").toLowerCase();
    return !["range", "button", "checkbox", "radio", "submit"].includes(type);
  }
  return false;
}

export function handleKeydown(event, actions) {
  if (event.metaKey || event.ctrlKey || event.altKey) return false;
  if (isSearchTarget(event.target)) {
    if (event.key === "Enter") {
      event.preventDefault();
      actions.onEnter();
      return true;
    }
    if (event.key === "Escape") {
      event.preventDefault();
      actions.blurSearch();
      return true;
    }
    if (event.key === "ArrowUp" || event.key === "ArrowDown") {
      event.preventDefault();
      actions.moveSelection(event.key === "ArrowUp" ? -1 : 1);
      return true;
    }
    return false;
  }
  if (isTypingTarget(event.target)) return false;
  if (event.target?.tagName === "INPUT" && event.target.type === "range") return false;
  if (
    typeof event.target?.closest === "function" &&
    event.target.closest("button") &&
    (event.key === "Enter" || event.key === " ")
  ) {
    return false;
  }
  const binding = bindingFor(event);
  if (!binding) return false;
  event.preventDefault();
  const fn = actions[binding.action];
  if (typeof fn === "function") fn(...(binding.args || []));
  return true;
}
