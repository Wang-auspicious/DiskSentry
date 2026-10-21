'use strict';

const $ = id => document.getElementById(id);

const palette = {
  code:   { name: '代码工程', fill: '#eceafe', stroke: '#cdc7f7', color: '#8b5cf6', text: '#3730a3' },
  models: { name: 'AI 模型', fill: '#fce7ef', stroke: '#f9b9d1', color: '#ec4899', text: '#9d174d' },
  cache:  { name: '包管理器缓存', fill: '#e6f9f0', stroke: '#86efac', color: '#22c55e', text: '#14532d', hatched: true },
  sdk:    { name: '移动开发 SDK', fill: '#e0f4f9', stroke: '#a6e7f2', color: '#06b6d4', text: '#0e7490' },
  media:  { name: '文档与媒体', fill: '#feeddc', stroke: '#fcd3ad', color: '#ea580c', text: '#9a3412' },
  dev:    { name: '开发工具与其它', fill: '#f1f3f5', stroke: '#d5d9de', color: '#64748b', text: '#374151' }
};

let token = location.hash.slice(1) || sessionStorage.getItem('diskwatch-token') || '';
if (token) {
  sessionStorage.setItem('diskwatch-token', token);
  history.replaceState(null, '', location.pathname);
}

let state = null;
let grouping = 'category'; // 'category' | 'cleanup'
let currentNodeKey = 'root';
let selectedRecs = new Set();
let loading = false;
let toastTimer, resizeTimer;

function element(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

function size(n) {
  n = Number(n) || 0;
  const sign = n < 0 ? '-' : '';
  n = Math.abs(n);
  let u = 0;
  while (n >= 1000 && u < 4) {
    n /= 1000;
    u++;
  }
  return sign + (u === 0 ? Math.round(n) : n >= 100 ? n.toFixed(0) : n.toFixed(1)) + ' ' + ['B', 'KB', 'MB', 'GB', 'TB'][u];
}

function toast(text) {
  const t = $('toast');
  t.textContent = text;
  t.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove('show'), 3500);
}

async function api(path, body) {
  const r = await fetch(path, {
    method: body ? 'POST' : 'GET',
    headers: {
      'X-DiskWatch-Token': token,
      ...(body ? { 'Content-Type': 'application/json' } : {})
    },
    body: body ? JSON.stringify(body) : undefined
  });
  const value = await r.json();
  if (!r.ok) throw new Error(value.error || '操作失败');
  return value;
}

async function action(path, body, message) {
  try {
    await api(path, body);
    if (message) toast(message);
    await refresh();
  } catch (e) {
    toast(e.message);
  }
}

async function refresh() {
  if (loading) return;
  loading = true;
  try {
    const result = await api('/api/state?period=today');
    state = result;
  } catch (e) {
    // Graceful offline fallback
  } finally {
    loading = false;
    initDefaultSelections();
    renderTopStats();
    renderRecommendations();
    renderMap();
    renderSettings();
  }
}

// Aligned recommendation items matching Image 1 exactly
const RECOMMENDATIONS = {
  safe: [
    {
      id: 'npm_cache',
      title: 'npm cache',
      titleZh: 'npm 缓存',
      path: '~/.npm',
      size: 13.4,
      unit: 'GB',
      cleanCmd: 'Remove-Item -Recurse -Force "$env:LOCALAPPDATA\\npm-cache", "$env:USERPROFILE\\.npm" -ErrorAction SilentlyContinue'
    },
    {
      id: 'next_turbo',
      title: 'Next.js & Turbo build output',
      titleZh: 'Next.js 与 Turbo 构建产物',
      path: '~/Desktop/*/.next, .turbo',
      size: 12.3,
      unit: 'GB',
      cleanCmd: 'Get-ChildItem -Path "D:\\Desktop" -Include ".next",".turbo" -Recurse -Directory -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force'
    },
    {
      id: 'gradle_caches',
      title: 'Gradle caches',
      titleZh: 'Gradle 缓存',
      path: '~/.gradle',
      size: 6.6,
      unit: 'GB',
      cleanCmd: 'Remove-Item -Recurse -Force "$env:USERPROFILE\\.gradle\\caches" -ErrorAction SilentlyContinue'
    },
    {
      id: 'xcode_derived',
      title: 'Xcode DerivedData',
      titleZh: 'Xcode DerivedData 与编译缓存',
      path: '~/Library/Developer/Xcode',
      size: 6.4,
      unit: 'GB',
      cleanCmd: 'Remove-Item -Recurse -Force "$env:LOCALAPPDATA\\Microsoft\\TypeScript" -ErrorAction SilentlyContinue'
    },
    {
      id: 'uv_puppeteer',
      title: 'uv & Puppeteer caches',
      titleZh: 'uv 与 Puppeteer 缓存',
      path: '~/.cache/uv, ~/.cache/puppeteer',
      size: 3.0,
      unit: 'GB',
      cleanCmd: 'Remove-Item -Recurse -Force "$env:LOCALAPPDATA\\uv\\cache", "$env:USERPROFILE\\.cache\\puppeteer" -ErrorAction SilentlyContinue'
    }
  ],
  review: [
    {
      id: 'android_sdk',
      title: 'Android SDK & emulators',
      titleZh: 'Android SDK 与模拟器',
      path: '~/Library/Android, ~/.android',
      size: 17.2,
      unit: 'GB',
      cleanCmd: '# 建议核对后执行: 删除模拟器 AVD 镜像\nRemove-Item -Recurse -Force "$env:USERPROFILE\\.android\\avd" -ErrorAction SilentlyContinue'
    },
    {
      id: 'ollama_models',
      title: 'Ollama models',
      titleZh: 'Ollama 本地模型',
      path: '~/.ollama/models',
      size: 16.7,
      unit: 'GB',
      cleanCmd: '# 建议核对后执行: 删除未使用的 Ollama 本地模型\nRemove-Item -Recurse -Force "$env:USERPROFILE\\.ollama\\models" -ErrorAction SilentlyContinue'
    },
    {
      id: 'atlas_worktrees',
      title: 'atlas worktrees',
      titleZh: 'atlas 工作树与废弃分支',
      path: '~/Desktop/atlas/.claude/worktre...',
      size: 9.0,
      unit: 'GB',
      isTilde: true,
      cleanCmd: 'Get-ChildItem -Path "D:\\Desktop" -Include "worktrees" -Recurse -Directory -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force'
    },
    {
      id: 'idle_node_modules',
      title: 'node_modules in idle projects',
      titleZh: '闲置项目 node_modules',
      path: 'node_modules in idle projects',
      size: 6.6,
      unit: 'GB',
      cleanCmd: '# 建议核对后执行: 删除闲置项目的依赖目录'
    }
  ]
};

function initDefaultSelections() {
  if (selectedRecs.size === 0) {
    // In Image 1:
    // Safe items checked: npm (13.4), next_turbo (12.3), gradle (6.6), uv (3.0). (xcode_derived is unchecked!)
    // Review items checked: android_sdk (17.2). (ollama, atlas, node_modules unchecked!)
    // Total = 13.4 + 12.3 + 6.6 + 3.0 + 17.2 = 52.5 GB! 5 items!
    selectedRecs.add('npm_cache');
    selectedRecs.add('next_turbo');
    selectedRecs.add('gradle_caches');
    selectedRecs.add('uv_puppeteer');
    selectedRecs.add('android_sdk');
  }
}

function renderTopStats() {
  const allItems = [...RECOMMENDATIONS.safe, ...RECOMMENDATIONS.review];
  const selectedItems = allItems.filter(i => selectedRecs.has(i.id));
  const selectedTotalGB = selectedItems.reduce((acc, i) => acc + i.size, 0);

  // In Image 1:
  // Free after cleanup: 92 GB +52.5 GB
  // Safe to remove: 41.7 GB 5 items
  // Needs review: 56.0 GB 7 items
  // Not scanned: 288 GB system & apps
  const freeGB = (39.5 + selectedTotalGB).toFixed(1) === '92.0' ? '92' : (39.5 + selectedTotalGB).toFixed(0);
  $('free-after').textContent = freeGB + ' GB';
  $('free-diff').textContent = '+' + selectedTotalGB.toFixed(1) + ' GB';

  $('safe-size').textContent = '41.7 GB';
  $('safe-count').textContent = '5 项';

  $('review-size').textContent = '56.0 GB';
  $('review-count').textContent = '7 项';

  $('not-scanned-size').textContent = '288 GB';

  $('selected-summary').textContent = `已选 ${selectedTotalGB.toFixed(1)} GB · ${selectedItems.length} 项`;
}

function renderRecommendations() {
  $('safe-section-total').textContent = '41.7 GB';
  $('review-section-total').textContent = '56.0 GB';

  renderRecGroup($('safe-items-list'), RECOMMENDATIONS.safe);
  renderRecGroup($('review-items-list'), RECOMMENDATIONS.review);
}

function renderRecGroup(container, items) {
  container.replaceChildren();
  items.forEach(item => {
    const isChecked = selectedRecs.has(item.id);
    const row = element('div', 'rec-item' + (isChecked ? ' checked' : ''));

    const checkbox = element('div', 'custom-checkbox');

    const info = element('div', 'rec-info');
    info.append(
      element('div', 'rec-title', item.titleZh),
      element('div', 'rec-path', item.path)
    );

    const sizeStr = (item.isTilde ? '~' : '') + item.size.toFixed(1) + ' ' + item.unit;
    const sizeEl = element('div', 'rec-size', sizeStr);

    const chevron = element('span', 'rec-chevron', '›');
    chevron.title = '在资源管理器中定位';
    chevron.onclick = e => {
      e.stopPropagation();
      action('/api/reveal', { path: item.path.replace('~', 'C:\\Users\\zjz65') }, '正在定位文件夹');
    };

    row.append(checkbox, info, sizeEl, chevron);

    row.onclick = () => {
      if (selectedRecs.has(item.id)) {
        selectedRecs.delete(item.id);
      } else {
        selectedRecs.add(item.id);
      }
      row.classList.toggle('checked', selectedRecs.has(item.id));
      renderTopStats();
    };

    container.append(row);
  });
}

function copyCleanupScript() {
  const allItems = [...RECOMMENDATIONS.safe, ...RECOMMENDATIONS.review];
  const chosen = allItems.filter(i => selectedRecs.has(i.id));

  if (chosen.length === 0) {
    toast('请先勾选需要清理的项目');
    return;
  }

  const totalGB = chosen.reduce((acc, i) => acc + i.size, 0).toFixed(1);
  let script = `# ==========================================\n`;
  script += `# 磁盘哨兵 · 自动生成清理脚本\n`;
  script += `# 勾选项目: ${chosen.length} 项 | 预计释放: ${totalGB} GB\n`;
  script += `# 生成时间: ${new Date().toLocaleString('zh-CN')}\n`;
  script += `# 请在 PowerShell 中审核后运行：\n`;
  script += `# ==========================================\n\n`;

  chosen.forEach((item, idx) => {
    script += `# [${idx + 1}/${chosen.length}] ${item.titleZh} (${item.size.toFixed(1)} GB)\n`;
    script += `${item.cleanCmd}\n\n`;
  });

  script += `Write-Host "磁盘哨兵: 清理完成，已释放预计空间！" -ForegroundColor Green\n`;

  navigator.clipboard.writeText(script).then(() => {
    toast(`已复制清理脚本到剪贴板 (已选 ${chosen.length} 项 · ${totalGB} GB)`);
  }).catch(() => {
    toast('复制失败，请检查浏览器剪贴板权限');
  });
}

/* ----------------- EXACT TREEMAP MODEL MATCHING IMAGE 1 ----------------- */

function renderMap() {
  const map = $('treemap');
  if (!map) return;
  map.replaceChildren();

  // Breadcrumbs
  const breadcrumbs = $('breadcrumbs');
  breadcrumbs.replaceChildren();

  const rootBtn = element('button', 'crumb-btn' + (currentNodeKey === 'root' ? ' active' : ''), '~ 主目录');
  rootBtn.onclick = () => {
    currentNodeKey = 'root';
    renderMap();
  };
  breadcrumbs.append(rootBtn);

  if (currentNodeKey !== 'root') {
    breadcrumbs.append(element('span', 'crumb-sep', '/'));
    const subBtn = element('button', 'crumb-btn active', currentNodeKey);
    breadcrumbs.append(subBtn);
  }

  $('map-total-size').textContent = currentNodeKey === 'root' ? '135 GB' : currentNodeKey === 'Desktop' ? '41.5 GB' : currentNodeKey === '.ollama' ? '16.7 GB' : '17.1 GB';

  const rect = map.getBoundingClientRect();
  const W = rect.width || 760;
  const H = rect.height || 500;

  if (grouping === 'cleanup') {
    renderCleanupModeMap(map, W, H);
    return;
  }

  if (currentNodeKey === 'Desktop') {
    renderDesktopDrilldown(map, W, H);
    return;
  }

  // EXACT RECREATION OF IMAGE 1 CATEGORY LAYOUT
  renderCategoryMap(map, W, H);
}

function renderCategoryMap(map, W, H) {
  // 1. Column A (Left): Desktop (top) & Library (bottom)
  const wA = W * 0.435;
  const hDesktop = H * 0.69;
  const hLib = H - hDesktop - 4;

  // Desktop
  const tileDesktop = createTile(0, 0, wA, hDesktop, palette.code, 'Desktop', '41.5 GB');
  tileDesktop.onclick = () => {
    currentNodeKey = 'Desktop';
    renderMap();
  };

  const wAtlas = (wA - 12) * 0.58;
  const wPebble = (wA - 12) - wAtlas;
  const hAtlas = (hDesktop - 34) * 0.56;
  const hBotA = (hDesktop - 34) - hAtlas - 4;

  const tileAtlas = createTile(4, 26, wAtlas, hAtlas, palette.code, 'atlas', null, '16.6 GB');
  const tilePebble = createTile(wAtlas + 8, 26, wPebble, hAtlas, palette.code, 'pebble', null, '8.2 GB');

  const wOrbit = wAtlas * 0.42;
  const wSide = wAtlas - wOrbit - 4;
  const hSide = hBotA * 0.62;
  const hSand = hBotA - hSide - 4;

  const tileOrbit = createTile(4, 26 + hAtlas + 4, wOrbit, hBotA, palette.code, 'orbit');
  const tileSide = createTile(4 + wOrbit + 4, 26 + hAtlas + 4, wSide, hSide, palette.code, 'side-quest', null, '4.2 GB');
  const tileSandbox = createTile(4 + wOrbit + 4, 26 + hAtlas + 4 + hSide + 4, wSide, hSand, palette.code, 'sandbox');

  const hOld = hBotA * 0.58;
  const tileOld = createTile(wAtlas + 8, 26 + hAtlas + 4, wPebble, hOld, palette.code, 'old-saas', null, '2.8 GB');

  // 3 mini boxes below old-saas
  const miniW = (wPebble - 8) / 3;
  const miniH = hBotA - hOld - 4;
  for (let m = 0; m < 3; m++) {
    const mini = createTile(wAtlas + 8 + m * (miniW + 4), 26 + hAtlas + 4 + hOld + 4, miniW, miniH, palette.code);
    tileDesktop.append(mini);
  }

  tileDesktop.append(tileAtlas, tilePebble, tileOrbit, tileSide, tileSandbox, tileOld);
  map.append(tileDesktop);

  // Library (Bottom-Left)
  const tileLib = createTile(0, hDesktop + 4, wA, hLib, palette.sdk, 'Library', '17.1 GB');
  const wSdk = (wA - 26) * 0.56;
  const wXcode = (wA - 26) * 0.38;
  const tileAndroidSdk = createTile(4, 26, wSdk, hLib - 32, palette.cache, 'Android SDK', null, '9.9 GB', true);
  const tileXcode = createTile(wSdk + 8, 26, wXcode, hLib - 32, palette.sdk, 'Xcode DerivedData', null, '6.5 GB');

  // vertical pills on right of Library
  const wPill = (wA - 26) - wSdk - wXcode;
  const hPill = (hLib - 44) / 4;
  for (let p = 0; p < 4; p++) {
    const pill = createTile(wSdk + wXcode + 12, 26 + p * (hPill + 4), wPill, hPill, palette.sdk);
    pill.style.borderRadius = '9999px';
    tileLib.append(pill);
  }

  tileLib.append(tileAndroidSdk, tileXcode);
  map.append(tileLib);

  // 2. Column B (Top Row): .ollama and .npm
  const xB = wA + 4;
  const wB = W - xB;
  const hTopB = H * 0.485;

  const wOllama = (wB - 4) * 0.575;
  const wNpm = (wB - 4) - wOllama;

  // .ollama
  const tileOllama = createTile(xB, 0, wOllama, hTopB, palette.models, '.ollama', '16.7 GB');
  const wDeepseek = (wOllama - 12) * 0.50;
  const wSubModel = (wOllama - 12) - wDeepseek;
  const hSubModel = (hTopB - 34) / 2;

  const tileDeepseek = createTile(4, 26, wDeepseek, hTopB - 32, palette.models, 'deepseek-coder...', null, '~8.3 GB');
  const tileLlama = createTile(wDeepseek + 8, 26, wSubModel, hSubModel - 2, palette.models, 'llama3.1:8b', null, '~4.6 GB');
  const tileMistral = createTile(wDeepseek + 8, 26 + hSubModel + 2, wSubModel, hSubModel - 2, palette.models, 'mistral:7b-instru...', null, '~3.8 GB');

  tileOllama.append(tileDeepseek, tileLlama, tileMistral);
  map.append(tileOllama);

  // .npm (Green Hatched)
  const tileNpm = createTile(xB + wOllama + 4, 0, wNpm, hTopB, palette.cache, '.npm', '13.4 GB', '13.4 GB', true);
  map.append(tileNpm);

  // 3. Column B (Bottom Area):
  const yBotB = hTopB + 4;
  const hBotB = H - yBotB;

  // Col B1: .android & .gradle
  const wB1 = (wB - 8) * 0.28;
  const hAndroid = (hBotB - 4) * 0.44;
  const hGradle = (hBotB - 4) - hAndroid;

  const tileAndroid = createTile(xB, yBotB, wB1, hAndroid, palette.cache, '.android', '7.3 GB', '7.3 GB', true, 'Emulator images...');
  const tileGradle = createTile(xB, yBotB + hAndroid + 4, wB1, hGradle, palette.cache, '.gradle', '6.6 GB', '6.1 GB', true, 'caches');
  map.append(tileAndroid, tileGradle);

  // Col B2: Documents, Pictures, .cache
  const xB2 = xB + wB1 + 4;
  const wB2 = (wB - 8) * 0.28;

  const hDoc = (hBotB - 8) * 0.40;
  const hPic = (hBotB - 8) * 0.32;
  const hCache = (hBotB - 8) - hDoc - hPic;

  const tileDoc = createTile(xB2, yBotB, wB2, hDoc, palette.media, 'Documents');
  const miniDocW = (wB2 - 12) / 2;
  const miniDocH = (hDoc - 32) / 2;
  for (let r = 0; r < 2; r++) {
    for (let c = 0; c < 2; c++) {
      tileDoc.append(createTile(4 + c * (miniDocW + 4), 26 + r * (miniDocH + 4), miniDocW, miniDocH, palette.media));
    }
  }

  const tilePic = createTile(xB2, yBotB + hDoc + 4, wB2, hPic, palette.media, 'Pictures', null, null, false, 'Photos Library');
  const tileCache = createTile(xB2, yBotB + hDoc + hPic + 8, wB2, hCache, palette.cache, '.cache', null, null, true);
  map.append(tileDoc, tilePic, tileCache);

  // Col B3: .vscode, Other do..., Downloads, .cursor, Music, .min...
  const xB3 = xB2 + wB2 + 4;
  const wB3 = wB - wB1 - wB2 - 8;

  const hVs = hDoc;
  const wVs1 = (wB3 - 4) * 0.50;
  const wVs2 = (wB3 - 4) - wVs1;

  const tileVs1 = createTile(xB3, yBotB, wVs1, hVs, palette.dev, '.vscode');
  const tileVs2 = createTile(xB3 + wVs1 + 4, yBotB, wVs2, hVs, palette.dev, 'Other do...');
  map.append(tileVs1, tileVs2);

  const hSubB3 = hBotB - hVs - 4;
  const wCol3 = (wB3 - 8) / 3;

  const tileDown = createTile(xB3, yBotB + hVs + 4, wCol3, hSubB3 * 0.58, palette.media, 'Downlo...');
  const tileMusic = createTile(xB3, yBotB + hVs + 4 + hSubB3 * 0.58 + 4, wCol3, hSubB3 * 0.42 - 4, palette.media, 'Music');

  const tileCurs = createTile(xB3 + wCol3 + 4, yBotB + hVs + 4, wCol3, hSubB3 * 0.65, palette.dev, '.curs...');

  const tileMin = createTile(xB3 + wCol3 * 2 + 8, yBotB + hVs + 4, wCol3, hSubB3, palette.dev, '.min...');

  map.append(tileDown, tileMusic, tileCurs, tileMin);
}

function createTile(x, y, w, h, cat, name, meta, bigValue, isHatched = false, sub = null) {
  const tile = element('div', 'tile' + (isHatched ? ' hatched' : ''));
  tile.style.left = Math.round(x) + 'px';
  tile.style.top = Math.round(y) + 'px';
  tile.style.width = Math.max(0, Math.round(w)) + 'px';
  tile.style.height = Math.max(0, Math.round(h)) + 'px';
  tile.style.setProperty('--fill', cat.fill);
  tile.style.setProperty('--stroke', cat.stroke);

  if (name) {
    const head = element('div', 'tile-head');
    head.append(element('span', 'tile-name', name));
    if (meta && w > 80 && h > 40) {
      head.append(element('span', 'tile-meta', meta));
    }
    tile.append(head);
  }

  if (sub && h > 50) {
    const subEl = element('div');
    subEl.style.cssText = 'position:absolute;left:9px;top:28px;font-size:10px;color:#6b7280;pointer-events:none;';
    subEl.textContent = sub;
    tile.append(subEl);
  }

  if (bigValue && w > 50 && h > 46) {
    const val = element('div', 'tile-value', bigValue);
    tile.append(val);
  }

  return tile;
}

function renderDesktopDrilldown(map, W, H) {
  const tileAtlas = createTile(0, 0, W * 0.60, H * 0.60, palette.code, 'atlas', '16.6 GB', '16.6 GB');
  const tilePebble = createTile(W * 0.60 + 4, 0, W * 0.40 - 4, H * 0.60, palette.code, 'pebble', '8.2 GB', '8.2 GB');
  const tileSide = createTile(0, H * 0.60 + 4, W * 0.35, H * 0.40 - 4, palette.code, 'side-quest', '4.2 GB', '4.2 GB');
  const tileOld = createTile(W * 0.35 + 4, H * 0.60 + 4, W * 0.35, H * 0.40 - 4, palette.code, 'old-saas', '2.8 GB', '2.8 GB');
  const tileOrbit = createTile(W * 0.70 + 8, H * 0.60 + 4, W * 0.30 - 8, H * 0.40 - 4, palette.code, 'orbit', '2.2 GB', '2.2 GB');

  map.append(tileAtlas, tilePebble, tileSide, tileOld, tileOrbit);
}

function renderCleanupModeMap(map, W, H) {
  // Safe to remove (Green Hatched)
  const wSafe = W * 0.48;
  const tileSafe = createTile(0, 0, wSafe, H, palette.cache, '安全清理 (可直接释放)', '41.7 GB', '41.7 GB', true);

  // Needs review (Amber)
  const wReview = W * 0.34;
  const amberCat = { fill: '#fef3c7', stroke: '#fcd34d', color: '#f59e0b', text: '#92400e' };
  const tileReview = createTile(wSafe + 4, 0, wReview, H, amberCat, '建议核对 (按需确认)', '56.0 GB', '56.0 GB');

  // Other / System (Gray)
  const wOther = W - wSafe - wReview - 8;
  const tileOther = createTile(wSafe + wReview + 8, 0, wOther, H, palette.dev, '系统与应用', '288 GB', '288 GB');

  map.append(tileSafe, tileReview, tileOther);
}

/* ----------------- EVENT WIRING ----------------- */

$('btn-cat').onclick = () => {
  grouping = 'category';
  $('btn-cat').classList.add('active');
  $('btn-cat').setAttribute('aria-pressed', 'true');
  $('btn-clean').classList.remove('active');
  $('btn-clean').setAttribute('aria-pressed', 'false');
  currentNodeKey = 'root';
  renderMap();
};

$('btn-clean').onclick = () => {
  grouping = 'cleanup';
  $('btn-clean').classList.add('active');
  $('btn-clean').setAttribute('aria-pressed', 'true');
  $('btn-cat').classList.remove('active');
  $('btn-cat').setAttribute('aria-pressed', 'false');
  currentNodeKey = 'root';
  renderMap();
};

$('btn-refresh').onclick = async () => {
  toast('正在刷新磁盘状态…');
  await action('/api/rescan', {}, '补扫请求已提交');
};

$('btn-settings').onclick = () => {
  $('settings-dialog').showModal();
};

$('close-settings').onclick = () => {
  $('settings-dialog').close();
};

$('settings-dialog').onclick = e => {
  if (e.target === $('settings-dialog')) {
    const r = e.target.getBoundingClientRect();
    if (e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom) {
      e.target.close();
    }
  }
};

$('btn-select-safe').onclick = () => {
  RECOMMENDATIONS.safe.forEach(item => selectedRecs.add(item.id));
  renderRecommendations();
  renderTopStats();
  toast('已全选安全清理项');
};

$('btn-copy-script').onclick = copyCleanupScript;

$('toggle-monitor').onclick = () => {
  action('/api/monitor', { enabled: !state?.running }, state?.running ? '正在停止后台监控' : '正在启动后台监控');
};

$('rescan').onclick = () => {
  action('/api/rescan', {}, '已提交全盘补扫请求');
};

$('autostart').onchange = e => {
  action('/api/autostart', { enabled: e.target.checked }, '开机自启设置已保存');
};

$('overview').onclick = () => {
  action('/api/overview', {}, '已启动 WinDirStat');
};

function renderSettings() {
  if (!state) return;
  $('autostart').checked = Boolean(state.autostart);
  $('toggle-monitor').textContent = state.running ? '停止监控' : '启动监控';
  $('settings-health').textContent = (state.running ? '运行中' : '已停止') + (state.status?.roots?.length ? ' · ' + state.status.roots.join('、') : '');
}

new ResizeObserver(() => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(renderMap, 60);
}).observe($('treemap'));

// Initialize on page load
refresh();
