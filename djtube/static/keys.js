import { EQ_STEP } from "./eq.js";
import { FILTER_STEP } from "./filter.js";
import { RATE_STEP } from "./rate.js";

export const FADER_STEP = 0.04;
export const FADER_STEP_LARGE = 0.12;
export const VOLUME_STEP = 0.05;
export const JOG_STEP = 1;
export const JOG_STEP_LARGE = 10;

export const BINDINGS = [
  { keys: ["/"], action: "focusSearch", label: "検索にフォーカス", group: "検索" },
  { keys: ["Enter"], action: "onEnter", label: "検索", group: "検索" },
  { keys: ["Escape"], action: "blurSearch", label: "検索から抜ける", group: "検索" },
  { keys: ["ArrowUp", "k"], action: "moveSelection", args: [-1], label: "結果を上へ", group: "検索" },
  { keys: ["ArrowDown", "j"], action: "moveSelection", args: [1], label: "結果を下へ", group: "検索" },
  { keys: ["m"], action: "toggleMusicOnly", label: "音楽に限る", group: "検索" },
  { keys: ["a"], action: "loadSelected", args: ["A"], label: "デッキ A へロード", group: "デッキ" },
  { keys: ["b"], action: "loadSelected", args: ["B"], label: "デッキ B へロード", group: "デッキ" },
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
  { keys: ["1"], action: "nudgeRate", args: ["A", -RATE_STEP], label: "デッキ A のテンポを下げる", group: "テンポ" },
  { keys: ["2"], action: "nudgeRate", args: ["A", RATE_STEP], label: "デッキ A のテンポを上げる", group: "テンポ" },
  { keys: ["3"], action: "resetRate", args: ["A"], label: "デッキ A のテンポを 1.0 に戻す", group: "テンポ" },
  { keys: ["8"], action: "nudgeRate", args: ["B", -RATE_STEP], label: "デッキ B のテンポを下げる", group: "テンポ" },
  { keys: ["9"], action: "nudgeRate", args: ["B", RATE_STEP], label: "デッキ B のテンポを上げる", group: "テンポ" },
  { keys: ["0"], action: "resetRate", args: ["B"], label: "デッキ B のテンポを 1.0 に戻す", group: "テンポ" },
  { keys: ["3"], action: "syncBeat", args: ["A"], shift: true, label: "デッキ A の同期を入／切", group: "テンポ" },
  { keys: ["8"], action: "syncBeat", args: ["B"], shift: true, label: "デッキ B の同期を入／切", group: "テンポ" },
  { keys: ["-"], action: "nudgeVolume", args: ["A", -VOLUME_STEP], label: "デッキ A の音量を下げる", group: "音量" },
  { keys: ["="], action: "nudgeVolume", args: ["A", VOLUME_STEP], label: "デッキ A の音量を上げる", group: "音量" },
  {
    keys: [","],
    action: "nudgeVolume",
    args: ["B", -VOLUME_STEP],
    shift: true,
    label: "デッキ B の音量を下げる",
    group: "音量",
  },
  {
    keys: ["."],
    action: "nudgeVolume",
    args: ["B", VOLUME_STEP],
    shift: true,
    label: "デッキ B の音量を上げる",
    group: "音量",
  },
  { keys: ["e"], action: "nudgeEq", args: ["A", "high", -EQ_STEP], label: "デッキ A の HIGH を下げる", group: "イコライザー" },
  { keys: ["r"], action: "nudgeEq", args: ["A", "high", EQ_STEP], label: "デッキ A の HIGH を上げる", group: "イコライザー" },
  { keys: ["d"], action: "nudgeEq", args: ["A", "mid", -EQ_STEP], label: "デッキ A の MID を下げる", group: "イコライザー" },
  { keys: ["f"], action: "nudgeEq", args: ["A", "mid", EQ_STEP], label: "デッキ A の MID を上げる", group: "イコライザー" },
  { keys: ["c"], action: "nudgeEq", args: ["A", "low", -EQ_STEP], label: "デッキ A の LOW を下げる", group: "イコライザー" },
  { keys: ["v"], action: "nudgeEq", args: ["A", "low", EQ_STEP], label: "デッキ A の LOW を上げる", group: "イコライザー" },
  { keys: ["4"], action: "resetEq", args: ["A"], label: "デッキ A のイコライザーを 0 dB に戻す", group: "イコライザー" },
  { keys: ["i"], action: "nudgeEq", args: ["B", "high", -EQ_STEP], label: "デッキ B の HIGH を下げる", group: "イコライザー" },
  { keys: ["o"], action: "nudgeEq", args: ["B", "high", EQ_STEP], label: "デッキ B の HIGH を上げる", group: "イコライザー" },
  { keys: ["y"], action: "nudgeEq", args: ["B", "mid", -EQ_STEP], label: "デッキ B の MID を下げる", group: "イコライザー" },
  { keys: ["u"], action: "nudgeEq", args: ["B", "mid", EQ_STEP], label: "デッキ B の MID を上げる", group: "イコライザー" },
  { keys: ["n"], action: "nudgeEq", args: ["B", "low", -EQ_STEP], label: "デッキ B の LOW を下げる", group: "イコライザー" },
  { keys: ["h"], action: "nudgeEq", args: ["B", "low", EQ_STEP], label: "デッキ B の LOW を上げる", group: "イコライザー" },
  { keys: ["7"], action: "resetEq", args: ["B"], label: "デッキ B のイコライザーを 0 dB に戻す", group: "イコライザー" },
  { keys: ["t"], action: "nudgeFilter", args: ["A", -FILTER_STEP], label: "デッキ A のフィルターをローパス側へ", group: "フィルター" },
  {
    keys: ["t"],
    action: "nudgeFilter",
    args: ["A", FILTER_STEP],
    shift: true,
    label: "デッキ A のフィルターをハイパス側へ",
    group: "フィルター",
  },
  {
    keys: ["q"],
    action: "nudgeFilter",
    args: ["B", -FILTER_STEP],
    shift: true,
    label: "デッキ B のフィルターをローパス側へ",
    group: "フィルター",
  },
  {
    keys: ["w"],
    action: "nudgeFilter",
    args: ["B", FILTER_STEP],
    shift: true,
    label: "デッキ B のフィルターをハイパス側へ",
    group: "フィルター",
  },
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
  { keys: ["p"], action: "focusPlaylistName", label: "プレイリストの名前欄", group: "プレイリスト" },
  { keys: ["p"], action: "beginRenamePlaylist", shift: true, label: "プレイリストの名前を変える", group: "プレイリスト" },
  { keys: ["PageUp"], action: "cyclePlaylist", args: [-1], label: "前のプレイリスト", group: "プレイリスト" },
  { keys: ["PageDown"], action: "cyclePlaylist", args: [1], label: "次のプレイリスト", group: "プレイリスト" },
  { keys: ["l"], action: "addSearchHit", label: "検索の曲を追加", group: "プレイリスト" },
  { keys: ["s"], action: "addDeckTrack", args: ["A"], label: "デッキ A の曲を追加", group: "プレイリスト" },
  { keys: ["s"], action: "addDeckTrack", args: ["B"], shift: true, label: "デッキ B の曲を追加", group: "プレイリスト" },
  { keys: ["ArrowUp", "k"], action: "moveSelection", args: [-1], library: "playlist", label: "上の曲", group: "プレイリスト" },
  { keys: ["ArrowDown", "j"], action: "moveSelection", args: [1], library: "playlist", label: "下の曲", group: "プレイリスト" },
  { keys: ["g"], action: "movePlaylistSelection", args: [-1], label: "プレイリストの曲を上へ", group: "プレイリスト" },
  { keys: ["g"], action: "movePlaylistSelection", args: [1], shift: true, label: "プレイリストの曲を下へ", group: "プレイリスト" },
  { keys: ["5"], action: "movePlaylistTrack", args: [-1], label: "曲の順番を上げる", group: "プレイリスト" },
  { keys: ["6"], action: "movePlaylistTrack", args: [1], label: "曲の順番を下げる", group: "プレイリスト" },
  { keys: ["Backspace", "Delete"], action: "removePlaylistTrack", label: "プレイリストから外す", group: "プレイリスト" },
  {
    keys: ["Backspace", "Delete"],
    action: "deletePlaylist",
    shift: true,
    label: "プレイリストを消す",
    group: "プレイリスト",
  },
  { keys: ["a"], action: "loadPlaylistTrack", args: ["A"], shift: true, label: "プレイリストをデッキ A へ", group: "プレイリスト" },
  { keys: ["b"], action: "loadPlaylistTrack", args: ["B"], shift: true, label: "プレイリストをデッキ B へ", group: "プレイリスト" },
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
  PageUp: "PageUp",
  PageDown: "PageDown",
  Backspace: "Backspace",
  Delete: "Delete",
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
      const text = binding.shift ? `Shift+${label}` : label;
      if (binding.shift && key === ";" && shiftAliases(key).includes("+")) return `${text} +`;
      return text;
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
  "[": ["{"],
  "]": ["}"],
  ";": [":", "+"],
  "'": ['"'],
  ",": ["<"],
  ".": [">"],
  // Shift+3 is "#" on JIS and US. Shift+8 is "(" on JIS and "*" on US.
  "3": ["#"],
  "8": ["(", "*"],
};

function shiftAliases(bindingKey) {
  const aliases = SHIFT_ALIASES[bindingKey];
  return Array.isArray(aliases) ? aliases : [];
}

function sameKey(bindingKey, event) {
  if (event.key === bindingKey) return true;
  if (event.shiftKey && shiftAliases(bindingKey).includes(event.key)) return true;
  return (
    bindingKey.length === 1 &&
    event.key.length === 1 &&
    bindingKey.toLowerCase() === event.key.toLowerCase()
  );
}

export function bindingFor(event, library = "search") {
  if (event.metaKey || event.ctrlKey || event.altKey) return null;
  let fallback = null;
  for (const binding of BINDINGS) {
    if (!!binding.shift !== !!event.shiftKey) continue;
    if (!binding.keys.some((key) => sameKey(key, event))) continue;
    if (binding.library && binding.library !== library) continue;
    if (binding.library === library) return binding;
    if (!fallback) fallback = binding;
  }
  if (fallback) return fallback;
  // A JIS keyboard types "=" by shifting the minus key. That character is deck A volume up.
  if (event.shiftKey && event.key === "=") {
    return BINDINGS.find((binding) => !binding.shift && !binding.library && binding.keys.includes("=")) || null;
  }
  return null;
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

export function handleKeydown(event, actions, library = "search") {
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
  if (event.target?.id === "playlist-name") {
    if (event.key === "Enter") {
      event.preventDefault();
      if (typeof actions.submitPlaylistName === "function") actions.submitPlaylistName();
      return true;
    }
    if (event.key === "Escape") {
      event.preventDefault();
      if (typeof actions.blurPlaylistName === "function") actions.blurPlaylistName();
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
  const binding = bindingFor(event, library);
  if (!binding) return false;
  event.preventDefault();
  const fn = actions[binding.action];
  if (typeof fn === "function") fn(...(binding.args || []));
  return true;
}
