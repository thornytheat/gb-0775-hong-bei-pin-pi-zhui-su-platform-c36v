// 调拨单核对页的渐进式操作：提交后回刷服务端渲染的台账。
function parseOrigins(text) {
  const origins = {};
  String(text || '').split(/[,;，；]/).forEach((chunk) => {
    const part = chunk.trim();
    if (!part) return;
    const pair = part.split(/[:=：]/);
    if (pair.length === 2 && pair[0].trim() && pair[1].trim() !== '') {
      origins[pair[0].trim()] = pair[1].trim();
    }
  });
  return Object.keys(origins).length ? origins : undefined;
}

document.querySelectorAll('form[data-transfer-action]').forEach((form) => {
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const msg = form.querySelector('[data-msg]');
    const button = form.querySelector('button');
    if (msg) msg.textContent = '提交中…';
    if (button) button.disabled = true;

    const body = {};
    new FormData(form).forEach((value, key) => {
      if (key === 'origin_g_text') {
        const origins = parseOrigins(value);
        if (origins) body.origin_g = origins;
      } else if (String(value).trim() !== '') {
        body[key] = value;
      }
    });

    try {
      const response = await fetch(form.action, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const result = await response.json().catch(() => ({}));
      if (!response.ok) {
        if (msg) msg.textContent = result.message || result.code || '操作被拒绝';
        if (button) button.disabled = false;
        return;
      }
      if (msg) msg.textContent = result.reused ? '重复请求已按原结论处理，未重复落账。' : '已记账，正在刷新…';
      setTimeout(() => window.location.reload(), 350);
    } catch (error) {
      if (msg) msg.textContent = '网络异常，请重试（退单重试不会重复退料）。';
      if (button) button.disabled = false;
    }
  });
});
