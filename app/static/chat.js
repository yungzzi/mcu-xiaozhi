/* MCU 小智聊天前端：侧栏会话管理 + SSE 增量渲染 + 安全 Markdown（纯 DOM，无 innerHTML 拼接）。
 * 所有 API 用相对路径，兼容 /mcu-xiaozhi/ 反代前缀。 */
(function () {
  'use strict';
  var messagesEl = document.getElementById('messages');
  var convListEl = document.getElementById('conv-list');
  var convTitleEl = document.getElementById('conv-title');
  var bannerEl = document.getElementById('banner');
  var bannerText = document.getElementById('banner-text');
  var resendBtn = document.getElementById('resend');
  var inputEl = document.getElementById('input');
  var sendBtn = document.getElementById('send');
  var composer = document.getElementById('composer');
  var sidebar = document.getElementById('sidebar');
  var scrim = document.getElementById('scrim');
  var menuBtn = document.getElementById('menu-btn');
  var pendingEl = document.getElementById('pending');
  var fileInput = document.getElementById('file-input');

  var convId = null;
  var convsCache = [];
  var busy = false;
  var lastUserText = '';
  var lastAttachmentIds = [];
  var pending = [];

  var CHIP_SVG = '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><rect x="6" y="6" width="12" height="12" rx="2"/><rect x="10" y="10" width="4" height="4"/><path d="M9 6V3M15 6V3M9 21v-3M15 21v-3M6 9H3M6 15H3M21 9h-3M21 15h-3"/></svg>';
  var TRASH_SVG = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M4 7h16M9 7V5h6v2M6.5 7l1 13h9l1-13M10 11v6M14 11v6"/></svg>';

  /* ---------- 安全 Markdown：纯 DOM 构造 ---------- */
  var BASE = location.pathname.replace(/[^/]*$/, '');
  function safeUrl(u) {
    if (/^https?:\/\//i.test(u)) return u;
    if (u.charAt(0) === '/' && u.charAt(1) !== '/') return BASE + u.slice(1);
    return null;
  }
  function makeImage(url, alt, cls) {
    var img = document.createElement('img');
    img.className = cls;
    img.src = url;
    img.alt = alt || '';
    img.loading = 'lazy';
    img.addEventListener('error', function () {
      var a = document.createElement('a');
      a.href = url; a.target = '_blank'; a.rel = 'noopener noreferrer';
      a.textContent = '图片加载失败，点击查看原图链接';
      img.replaceWith(a);
    });
    return img;
  }
  var INLINE_RE = /!\[([^\]]*)\]\(([^)\s]+)\)|\[([^\]]+)\]\(([^)\s]+)\)|`([^`\n]+)`|\*\*([^*\n]+)\*\*|\n/g;
  function renderInlineInto(container, text) {
    var last = 0, m;
    INLINE_RE.lastIndex = 0;
    while ((m = INLINE_RE.exec(text)) !== null) {
      if (m.index > last) container.appendChild(document.createTextNode(text.slice(last, m.index)));
      if (m[2] !== undefined) {
        var iu = safeUrl(m[2]);
        if (iu) container.appendChild(makeImage(iu, m[1], 'plugin-img'));
        else container.appendChild(document.createTextNode(m[1] || m[2]));
      } else if (m[4] !== undefined) {
        var lu = safeUrl(m[4]);
        if (lu) {
          var a = document.createElement('a');
          a.href = lu; a.target = '_blank'; a.rel = 'noopener noreferrer';
          a.textContent = m[3];
          container.appendChild(a);
        } else container.appendChild(document.createTextNode(m[3]));
      } else if (m[5] !== undefined) {
        var c = document.createElement('code');
        c.className = 'inline'; c.textContent = m[5];
        container.appendChild(c);
      } else if (m[6] !== undefined) {
        var b = document.createElement('strong');
        b.textContent = m[6];
        container.appendChild(b);
      } else container.appendChild(document.createElement('br'));
      last = m.index + m[0].length;
    }
    if (last < text.length) container.appendChild(document.createTextNode(text.slice(last)));
  }
  function makeCodeBlock(lang, code) {
    var wrap = document.createElement('div');
    wrap.className = 'code-wrap';
    var head = document.createElement('div');
    head.className = 'code-head';
    var label = document.createElement('span');
    label.textContent = lang || 'code';
    var copy = document.createElement('button');
    copy.type = 'button';
    copy.className = 'code-copy';
    copy.textContent = '复制';
    copy.addEventListener('click', function () {
      var done = function () {
        copy.textContent = '已复制 ✓';
        setTimeout(function () { copy.textContent = '复制'; }, 1600);
      };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(code).then(done, done);
      } else {
        var ta = document.createElement('textarea');
        ta.value = code; document.body.appendChild(ta);
        ta.select(); document.execCommand('copy'); ta.remove(); done();
      }
    });
    head.appendChild(label); head.appendChild(copy);
    var pre = document.createElement('pre');
    pre.className = 'code';
    var codeEl = document.createElement('code');
    codeEl.textContent = code.replace(/\n$/, '');
    pre.appendChild(codeEl);
    wrap.appendChild(head); wrap.appendChild(pre);
    return wrap;
  }
  function renderMarkdownInto(container, src) {
    container.textContent = '';
    var lines = src.split('\n');
    var i = 0;
    while (i < lines.length) {
      var line = lines[i];
      var fence = line.match(/^```([\w+-]*)\s*$/);
      if (fence) {
        var buf = [];
        i++;
        while (i < lines.length && !/^```\s*$/.test(lines[i])) { buf.push(lines[i]); i++; }
        i++;
        container.appendChild(makeCodeBlock(fence[1], buf.join('\n')));
        continue;
      }
      var h = line.match(/^(#{1,3})\s+(.*)$/);
      if (h) {
        var hh = document.createElement('h' + h[1].length);
        renderInlineInto(hh, h[2]);
        container.appendChild(hh); i++; continue;
      }
      if (/^>\s?/.test(line)) {
        var bq = document.createElement('blockquote');
        var qbuf = [];
        while (i < lines.length && /^>\s?/.test(lines[i])) { qbuf.push(lines[i].replace(/^>\s?/, '')); i++; }
        renderInlineInto(bq, qbuf.join('\n'));
        container.appendChild(bq); continue;
      }
      if (/^[-*]\s+/.test(line)) {
        var ul = document.createElement('ul');
        while (i < lines.length && /^[-*]\s+/.test(lines[i])) {
          var li = document.createElement('li');
          renderInlineInto(li, lines[i].replace(/^[-*]\s+/, ''));
          ul.appendChild(li); i++;
        }
        container.appendChild(ul); continue;
      }
      if (/^\d+[.)]\s+/.test(line)) {
        var ol = document.createElement('ol');
        while (i < lines.length && /^\d+[.)]\s+/.test(lines[i])) {
          var li2 = document.createElement('li');
          renderInlineInto(li2, lines[i].replace(/^\d+[.)]\s+/, ''));
          ol.appendChild(li2); i++;
        }
        container.appendChild(ol); continue;
      }
      if (line.trim() === '') { i++; continue; }
      var p = document.createElement('p');
      var pbuf = [];
      while (i < lines.length && lines[i].trim() !== '' &&
             !/^```/.test(lines[i]) && !/^(#{1,3})\s/.test(lines[i]) &&
             !/^>\s?/.test(lines[i]) && !/^[-*]\s+/.test(lines[i]) && !/^\d+[.)]\s+/.test(lines[i])) {
        pbuf.push(lines[i]); i++;
      }
      renderInlineInto(p, pbuf.join('\n'));
      container.appendChild(p);
    }
  }

  /* ---------- 消息渲染 ---------- */
  function bubble(role) {
    var div = document.createElement('div');
    div.className = 'msg ' + role;
    var av = document.createElement('span');
    av.className = 'avatar';
    if (role === 'assistant') av.innerHTML = CHIP_SVG;  /* 静态常量图标，非用户内容 */
    else av.textContent = '我';
    var body = document.createElement('div');
    body.className = 'body';
    div.appendChild(av); div.appendChild(body);
    messagesEl.appendChild(div);
    scrollBottom();
    return body;
  }
  function scrollBottom() { messagesEl.scrollTop = messagesEl.scrollHeight; }
  function clearEmpty() {
    var hero = messagesEl.querySelector('.empty-hero');
    if (hero) hero.remove();
  }
  function renderEmpty() {
    messagesEl.textContent = '';
    var hero = document.createElement('div');
    hero.className = 'empty-hero';
    var mark = document.createElement('span');
    mark.className = 'brand-mark';
    mark.innerHTML = CHIP_SVG;
    var h2 = document.createElement('h2');
    h2.textContent = '你好，我是 MCU 小智';
    var p = document.createElement('p');
    p.textContent = '绍兴大学单片机课程助手。电路原理、代码示例、课程配图，都可以问我。';
    var grid = document.createElement('div');
    grid.className = 'suggest-grid';
    [
      ['画一张原理图', '画一个51单片机最小系统版原理图'],
      ['讲清一个电路', '晶振电路在最小系统里起什么作用？负载电容怎么选？'],
      ['来一段代码', '给我一段 51 单片机 LED 闪烁的 C 代码示例']
    ].forEach(function (s) {
      var b = document.createElement('button');
      b.type = 'button';
      b.className = 'suggest';
      var t = document.createElement('strong');
      t.textContent = s[0];
      b.appendChild(t);
      b.appendChild(document.createTextNode(s[1]));
      b.addEventListener('click', function () { send(s[1], []); });
      grid.appendChild(b);
    });
    hero.appendChild(mark); hero.appendChild(h2); hero.appendChild(p); hero.appendChild(grid);
    messagesEl.appendChild(hero);
  }
  function renderStored(m) {
    var body = bubble(m.role);
    if (m.role === 'assistant') {
      renderMarkdownInto(body, m.content);
      if (m.status && m.status !== 'ok') {
        var tag = document.createElement('div');
        tag.className = 'status-tag';
        tag.textContent = statusText(m.status) + '（内容可能不完整）';
        body.appendChild(tag);
      }
    } else body.textContent = m.content;
    (m.attachments || []).forEach(function (a) {
      var img = document.createElement('img');
      img.className = 'attachment-thumb';
      img.src = 'api/attachments/' + a.id;
      img.alt = a.orig_name;
      img.loading = 'lazy';
      body.appendChild(img);
    });
  }
  function statusText(kind) {
    return {
      quota: '额度已耗尽',
      auth: '凭证失效，请联系管理员',
      interrupted: '回复中断',
      action_required: '智能体需要额外操作（如端插件授权），本轮未完成',
      network: '网络错误',
      other: '生成失败'
    }[kind] || '生成失败';
  }
  function showBanner(kind, message) {
    bannerText.textContent = statusText(kind) + (message ? '：' + message : '');
    bannerEl.hidden = false;
  }
  function hideBanner() { bannerEl.hidden = true; }

  /* ---------- 侧栏会话管理 ---------- */
  function renderConversations() {
    convListEl.textContent = '';
    if (!convsCache.length) {
      var e = document.createElement('div');
      e.className = 'conv-empty';
      e.textContent = '暂无聊天记录';
      convListEl.appendChild(e);
      return;
    }
    convsCache.forEach(function (c) {
      var item = document.createElement('button');
      item.type = 'button';
      item.className = 'conv-item' + (c.id === convId ? ' active' : '');
      var title = document.createElement('span');
      title.className = 'conv-title';
      title.textContent = c.title;
      var del = document.createElement('span');
      del.className = 'conv-del';
      del.setAttribute('role', 'button');
      del.setAttribute('aria-label', '删除这条聊天');
      del.innerHTML = TRASH_SVG;
      del.addEventListener('click', function (ev) {
        ev.stopPropagation();
        deleteConversation(c.id);
      });
      item.appendChild(title); item.appendChild(del);
      item.addEventListener('click', function () {
        closeDrawer();
        if (c.id !== convId) openConversation(c.id);
      });
      convListEl.appendChild(item);
    });
  }
  async function deleteConversation(cid) {
    if (!confirm('删除这条聊天记录？')) return;
    var r = await fetch('api/conversations/' + cid, { method: 'DELETE' });
    if (r.status === 409) { alert('当前聊天正在生成回复，请稍后再删除'); return; }
    if (!r.ok) { alert('删除失败（' + r.status + '），请稍后重试'); return; }
    convsCache = convsCache.filter(function (c) { return c.id !== cid; });
    if (cid === convId) {
      if (convsCache.length) openConversation(convsCache[0].id);
      else {
        var nc = await createConversation();
        convsCache = [nc];
        openConversation(nc.id);
      }
    }
    renderConversations();
  }
  async function createConversation() {
    var r = await fetch('api/conversations', { method: 'POST' });
    return r.json();
  }
  async function loadConversations(preferCid) {
    var r = await fetch('api/conversations');
    if (r.status === 401) { location.href = 'login'; return; }
    convsCache = await r.json();
    var target = preferCid || (convsCache[0] && convsCache[0].id);
    if (!target) {
      var nc = await createConversation();
      convsCache = [nc];
      target = nc.id;
    }
    renderConversations();
    await openConversation(Number(target));
  }
  async function openConversation(cid) {
    convId = cid;
    hideBanner();
    var cur = convsCache.filter(function (c) { return c.id === cid; })[0];
    convTitleEl.textContent = cur ? cur.title : '新对话';
    renderConversations();
    messagesEl.textContent = '';
    var r = await fetch('api/conversations/' + cid + '/messages');
    if (!r.ok) return;
    var data = await r.json();
    if (!data.messages.length) renderEmpty();
    else data.messages.forEach(renderStored);
    scrollBottom();
  }
  async function refreshConversations() {
    var r = await fetch('api/conversations');
    if (!r.ok) return;
    convsCache = await r.json();
    var cur = convsCache.filter(function (c) { return c.id === convId; })[0];
    if (cur) convTitleEl.textContent = cur.title;
    renderConversations();
  }

  /* ---------- 移动端抽屉 ---------- */
  function openDrawer() { sidebar.classList.add('open'); scrim.hidden = false; }
  function closeDrawer() { sidebar.classList.remove('open'); scrim.hidden = true; }
  menuBtn.addEventListener('click', openDrawer);
  scrim.addEventListener('click', closeDrawer);

  /* ---------- 待发送附件 ---------- */
  function renderPending() {
    pendingEl.textContent = '';
    pendingEl.hidden = pending.length === 0;
    pending.forEach(function (a) {
      var chip = document.createElement('span');
      chip.className = 'chip';
      var img = document.createElement('img');
      img.src = 'api/attachments/' + a.id;
      img.alt = a.orig_name;
      chip.appendChild(img);
      var rm = document.createElement('button');
      rm.type = 'button';
      rm.textContent = '×';
      rm.setAttribute('aria-label', '移除图片');
      rm.addEventListener('click', function () {
        pending = pending.filter(function (x) { return x.id !== a.id; });
        renderPending();
      });
      chip.appendChild(rm);
      pendingEl.appendChild(chip);
    });
  }
  document.getElementById('attach').addEventListener('click', function () { fileInput.click(); });
  fileInput.addEventListener('change', async function () {
    var f = fileInput.files[0];
    fileInput.value = '';
    if (!f) return;
    var fd = new FormData();
    fd.append('file', f);
    var r = await fetch('api/uploads', { method: 'POST', body: fd });
    if (!r.ok) {
      var err = await r.json().catch(function () { return {}; });
      showBanner('other', err.message || '图片上传失败（' + r.status + '）');
      return;
    }
    pending.push(await r.json());
    renderPending();
  });

  /* ---------- SSE 发送 ---------- */
  function setBusy(v) {
    busy = v;
    sendBtn.disabled = v;
    sendBtn.classList.toggle('busy', v);
    sendBtn.setAttribute('aria-label', v ? '生成中' : '发送');
  }
  function typingDots(body) {
    var t = document.createElement('span');
    t.className = 'typing';
    t.appendChild(document.createElement('i'));
    t.appendChild(document.createElement('i'));
    t.appendChild(document.createElement('i'));
    body.appendChild(t);
  }
  async function send(text, attachmentIds) {
    attachmentIds = attachmentIds || [];
    if (busy || !convId || (!text.trim() && attachmentIds.length === 0)) return;
    hideBanner();
    clearEmpty();
    lastUserText = text;
    lastAttachmentIds = attachmentIds;
    var userBody = bubble('user');
    userBody.textContent = text;
    attachmentIds.forEach(function (id) {
      var img = document.createElement('img');
      img.className = 'attachment-thumb';
      img.src = 'api/attachments/' + id;
      img.loading = 'lazy';
      userBody.appendChild(img);
    });
    var liveBody = bubble('assistant');
    liveBody.classList.add('streaming');
    typingDots(liveBody);
    setBusy(true);
    var acc = '';
    var finished = false;
    var started = false;
    try {
      var resp = await fetch('api/conversations/' + convId + '/send', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text: text, attachment_ids: attachmentIds })
      });
      if (resp.status === 409) {
        liveBody.parentElement.remove();
        showBanner('other', '当前聊天正在生成回复，请稍候');
        return;
      }
      if (!resp.ok) {
        liveBody.parentElement.remove();
        showBanner('other', '请求失败（' + resp.status + '）');
        return;
      }
      var reader = resp.body.getReader();
      var decoder = new TextDecoder();
      var buf = '';
      while (true) {
        var chunk = await reader.read();
        if (chunk.done) break;
        buf += decoder.decode(chunk.value, { stream: true });
        var parts = buf.split('\n\n');
        buf = parts.pop();
        for (var i = 0; i < parts.length; i++) {
          var ev = parseSse(parts[i]);
          if (!ev) continue;
          if (ev.event === 'delta') {
            if (!started) {
              started = true;
              liveBody.textContent = '';
              liveBody.classList.add('streaming');
            }
            acc += ev.data.text;
            liveBody.textContent = acc;
            scrollBottom();
          } else if (ev.event === 'done') {
            finished = true;
            liveBody.classList.remove('streaming');
            renderMarkdownInto(liveBody, ev.data.content);
            scrollBottom();
          } else if (ev.event === 'error') {
            finished = true;
            liveBody.classList.remove('streaming');
            if (acc) renderMarkdownInto(liveBody, acc);
            else liveBody.parentElement.remove();
            showBanner(ev.data.kind, ev.data.message);
          }
        }
      }
      if (!finished) {
        liveBody.classList.remove('streaming');
        if (acc) renderMarkdownInto(liveBody, acc);
        else liveBody.parentElement.remove();
        showBanner('interrupted', '连接结束但未收到完整回复');
      }
    } catch (e) {
      liveBody.classList.remove('streaming');
      if (acc) renderMarkdownInto(liveBody, acc);
      showBanner('network', '连接中断，可重发上一条');
    } finally {
      setBusy(false);
      refreshConversations();
    }
  }
  function parseSse(block) {
    var event = 'message', data = '';
    block.split('\n').forEach(function (line) {
      if (line.indexOf('event:') === 0) event = line.slice(6).trim();
      else if (line.indexOf('data:') === 0) data += line.slice(5).trim();
    });
    if (!data) return null;
    try { return { event: event, data: JSON.parse(data) }; }
    catch (e) { return null; }
  }

  /* ---------- 输入区 ---------- */
  function autogrow() {
    inputEl.style.height = 'auto';
    inputEl.style.height = Math.min(inputEl.scrollHeight, 160) + 'px';
  }
  inputEl.addEventListener('input', autogrow);
  composer.addEventListener('submit', function (e) {
    e.preventDefault();
    var text = inputEl.value;
    var ids = pending.map(function (a) { return a.id; });
    inputEl.value = '';
    autogrow();
    pending = [];
    renderPending();
    send(text, ids);
  });
  inputEl.addEventListener('keydown', function (e) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      composer.requestSubmit();
    }
  });
  document.getElementById('new-conv').addEventListener('click', async function () {
    closeDrawer();
    var nc = await createConversation();
    convsCache.unshift(nc);
    renderConversations();
    await openConversation(nc.id);
  });
  resendBtn.addEventListener('click', function () {
    hideBanner();
    if (lastUserText || lastAttachmentIds.length) send(lastUserText, lastAttachmentIds);
  });

  var params = new URLSearchParams(location.search);
  loadConversations(params.get('cid') ? Number(params.get('cid')) : null);
})();