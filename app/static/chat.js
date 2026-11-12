/* MCU 小智聊天前端：SSE 增量渲染 + 安全 Markdown（无第三方依赖）。
 * 所有 API 用相对路径，兼容 /mcu-xiaozhi/ 反代前缀。 */
(function () {
  'use strict';
  var messagesEl = document.getElementById('messages');
  var selectEl = document.getElementById('conv-select');
  var bannerEl = document.getElementById('banner');
  var bannerText = document.getElementById('banner-text');
  var resendBtn = document.getElementById('resend');
  var inputEl = document.getElementById('input');
  var sendBtn = document.getElementById('send');
  var composer = document.getElementById('composer');

  var convId = null;
  var busy = false;
  var lastUserText = '';

  /* ---------- 安全 Markdown：先整体转义，再做受限替换 ---------- */
  function escapeHtml(s) {
    return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }
  /* 当前页面目录（兼容 /mcu-xiaozhi/ 反代前缀），用于解析站内根相对路径 */
  var BASE = location.pathname.replace(/[^/]*$/, '');
  function safeUrl(u) {
    if (/^https?:\/\//i.test(u)) return u;
    if (u.charAt(0) === '/' && u.charAt(1) !== '/') return BASE + u.slice(1);
    return null;
  }
  function renderMarkdown(src) {
    var text = escapeHtml(src);
    var blocks = [];
    // 围栏代码块
    text = text.replace(/```([\w+-]*)\n?([\s\S]*?)```/g, function (_, lang, code) {
      blocks.push('<pre class="code"><code>' + code.replace(/\n$/, '') + '</code></pre>');
      return '\u0000B' + (blocks.length - 1) + '\u0000';
    });
    // 图片（仅 http/https；加载失败降级为链接，见 attachImageFallback）
    text = text.replace(/!\[([^\]]*)\]\(([^)\s]+)\)/g, function (_, alt, url) {
      var u = safeUrl(url);
      if (!u) return alt || url;
      return '<img class="plugin-img" src="' + u + '" alt="' + alt + '" loading="lazy">';
    });
    // 链接
    text = text.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, function (_, t, url) {
      var u = safeUrl(url);
      if (!u) return t;
      return '<a href="' + u + '" target="_blank" rel="noopener noreferrer">' + t + '</a>';
    });
    // 行内代码与加粗
    text = text.replace(/`([^`\n]+)`/g, '<code class="inline">$1</code>');
    text = text.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
    // 段落换行（代码块占位符前后不插 br）
    text = text.replace(/\n/g, '<br>');
    text = text.replace(/\u0000B(\d+)\u0000/g, function (_, i) { return blocks[Number(i)]; });
    return text;
  }
  function attachImageFallback(root) {
    root.querySelectorAll('img.plugin-img').forEach(function (img) {
      img.addEventListener('error', function () {
        var a = document.createElement('a');
        a.href = img.src;
        a.target = '_blank';
        a.rel = 'noopener noreferrer';
        a.textContent = '图片加载失败，点击查看原图链接';
        img.replaceWith(a);
      });
    });
  }

  /* ---------- 消息渲染 ---------- */
  function bubble(role) {
    var div = document.createElement('div');
    div.className = 'msg ' + role;
    var body = document.createElement('div');
    body.className = 'body';
    div.appendChild(body);
    messagesEl.appendChild(div);
    messagesEl.scrollTop = messagesEl.scrollHeight;
    return body;
  }
  function renderStored(m) {
    var body = bubble(m.role);
    if (m.role === 'assistant') {
      body.innerHTML = renderMarkdown(m.content);
      attachImageFallback(body);
      if (m.status && m.status !== 'ok') {
        var tag = document.createElement('div');
        tag.className = 'status-tag';
        tag.textContent = statusText(m.status) + '（内容可能不完整）';
        body.appendChild(tag);
      }
    } else {
      body.textContent = m.content;
    }
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
      network: '网络错误',
      other: '生成失败'
    }[kind] || '生成失败';
  }
  function showBanner(kind, message) {
    bannerText.textContent = statusText(kind) + (message ? '：' + message : '');
    bannerEl.hidden = false;
  }
  function hideBanner() { bannerEl.hidden = true; }

  /* ---------- 会话管理 ---------- */
  async function loadConversations(preferCid) {
    var r = await fetch('api/conversations');
    if (r.status === 401) { location.href = 'login'; return; }
    var convs = await r.json();
    selectEl.innerHTML = '';
    convs.forEach(function (c) {
      var opt = document.createElement('option');
      opt.value = c.id;
      opt.textContent = c.title;
      selectEl.appendChild(opt);
    });
    var target = preferCid || (convs[0] && convs[0].id);
    if (!target) {
      var cr = await fetch('api/conversations', { method: 'POST' });
      var nc = await cr.json();
      var opt = document.createElement('option');
      opt.value = nc.id; opt.textContent = nc.title;
      selectEl.appendChild(opt);
      target = nc.id;
    }
    selectEl.value = String(target);
    await openConversation(Number(target));
  }
  async function openConversation(cid) {
    convId = cid;
    hideBanner();
    messagesEl.innerHTML = '';
    var r = await fetch('api/conversations/' + cid + '/messages');
    if (!r.ok) return;
    var data = await r.json();
    data.messages.forEach(renderStored);
  }

  /* ---------- SSE 发送 ---------- */
  function setBusy(v) {
    busy = v;
    sendBtn.disabled = v;
    sendBtn.textContent = v ? '生成中…' : '发送';
  }
  async function send(text) {
    if (busy || !convId || !text.trim()) return;
    hideBanner();
    lastUserText = text;
    var userBody = bubble('user');
    userBody.textContent = text;
    var liveBody = bubble('assistant');
    liveBody.classList.add('streaming');
    setBusy(true);
    var acc = '';
    var finished = false;
    try {
      var resp = await fetch('api/conversations/' + convId + '/send', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text: text })
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
            acc += ev.data.text;
            liveBody.textContent = acc;  // 流式阶段纯文本，避免重复拼接渲染
            messagesEl.scrollTop = messagesEl.scrollHeight;
          } else if (ev.event === 'done') {
            finished = true;
            liveBody.classList.remove('streaming');
            liveBody.innerHTML = renderMarkdown(ev.data.content);
            attachImageFallback(liveBody);
          } else if (ev.event === 'error') {
            finished = true;
            liveBody.classList.remove('streaming');
            if (acc) {
              liveBody.innerHTML = renderMarkdown(acc);
              attachImageFallback(liveBody);
            } else {
              liveBody.parentElement.remove();
            }
            showBanner(ev.data.kind, ev.data.message);
          }
        }
      }
      if (!finished) {
        liveBody.classList.remove('streaming');
        if (acc) {
          liveBody.innerHTML = renderMarkdown(acc);
          attachImageFallback(liveBody);
        }
        showBanner('interrupted', '连接结束但未收到完整回复');
      }
    } catch (e) {
      liveBody.classList.remove('streaming');
      showBanner('network', '连接中断，可重发上一条');
    } finally {
      setBusy(false);
      refreshTitles();
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
  async function refreshTitles() {
    var r = await fetch('api/conversations');
    if (!r.ok) return;
    var convs = await r.json();
    var cur = selectEl.value;
    selectEl.innerHTML = '';
    convs.forEach(function (c) {
      var opt = document.createElement('option');
      opt.value = c.id; opt.textContent = c.title;
      selectEl.appendChild(opt);
    });
    selectEl.value = cur;
  }

  /* ---------- 事件绑定 ---------- */
  composer.addEventListener('submit', function (e) {
    e.preventDefault();
    var text = inputEl.value;
    inputEl.value = '';
    send(text);
  });
  inputEl.addEventListener('keydown', function (e) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      composer.requestSubmit();
    }
  });
  selectEl.addEventListener('change', function () {
    openConversation(Number(selectEl.value));
  });
  document.getElementById('new-conv').addEventListener('click', async function () {
    var r = await fetch('api/conversations', { method: 'POST' });
    var nc = await r.json();
    await loadConversations(nc.id);
  });
  resendBtn.addEventListener('click', function () {
    hideBanner();
    if (lastUserText) send(lastUserText);
  });

  var params = new URLSearchParams(location.search);
  loadConversations(params.get('cid') ? Number(params.get('cid')) : null);
})();