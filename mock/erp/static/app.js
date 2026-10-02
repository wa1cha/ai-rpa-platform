/* 模拟 ERP 的前端脚本 —— 克制到只有两件事。

   《模拟ERP设计》§2 明确「老系统基本没有前端逻辑」，所以这里不引入任何框架。
   剩下这两件事都不是「为了好看」，而是环境本身的一部分：
     1. 给 /api/* 请求带上防作弊头 —— 这是「RPA 禁止调 API」能成立的技术前提；
     2. 保存/提交前弹确认框 —— 这是 RPA 必须处理的异步 UI，一个真实的坑。
   库存实时校验是第三件事，它存在的意义是让第 1 条不是一段死代码。
*/
(function () {
  'use strict';

  // ---------- 1) 防作弊头 ----------
  // token 由服务端渲染进 <meta>，每次会话随机（§5.1）。
  var meta = document.querySelector('meta[name="erp-ui-token"]');
  var UI_TOKEN = meta ? meta.getAttribute('content') : '';

  // 包一层 fetch，而不是在每个调用点手写请求头 —— 后者迟早会漏一个，
  // 而漏掉的后果是「页面上的某个功能对 RPA 静默失效」，极难查。
  var nativeFetch = window.fetch;
  window.fetch = function (input, init) {
    var url = typeof input === 'string' ? input : (input && input.url) || '';
    if (url.indexOf('/api/') !== -1) {
      init = init || {};
      init.headers = Object.assign({}, init.headers, { 'X-ERP-UI': UI_TOKEN });
    }
    return nativeFetch.call(this, input, init);
  };

  // ---------- 2) 确认对话框 ----------
  var dialog = document.getElementById('confirm-dialog');
  var messageEl = document.getElementById('confirm-message');
  var yesBtn = document.getElementById('confirm-yes');
  var noBtn = document.getElementById('confirm-no');
  var pendingForm = null;

  document.querySelectorAll('form[data-confirm]').forEach(function (form) {
    form.addEventListener('submit', function (event) {
      event.preventDefault();
      pendingForm = form;
      messageEl.textContent = form.getAttribute('data-confirm') || '确定吗？';
      dialog.showModal();
    });
  });

  if (yesBtn) {
    yesBtn.addEventListener('click', function () {
      dialog.close();
      var form = pendingForm;
      pendingForm = null;
      // 用原生 submit()：它**不触发** submit 事件，所以不会再次弹框。
      if (form) { form.submit(); }
    });
  }
  if (noBtn) {
    noBtn.addEventListener('click', function () {
      pendingForm = null;
      dialog.close();
    });
  }

  // ---------- 3) 库存实时校验 ----------
  // 只在新建订单页存在这些元素时才挂。走 /api/inventory/check ——
  // 顺带证明了「带上头才调得通」这条链路是活的。
  var skuInput = document.querySelector('[data-testid="input-sku"]');
  var qtyInput = document.querySelector('[data-testid="input-quantity"]');
  var hint = document.getElementById('stock-hint');

  function checkStock() {
    if (!hint || !skuInput || !skuInput.value.trim()) { return; }
    var quantity = parseInt(qtyInput && qtyInput.value, 10);
    if (!quantity || quantity < 1) { quantity = 1; }

    fetch('/api/inventory/check', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ sku: skuInput.value.trim(), quantity: quantity })
    })
      .then(function (response) { return response.ok ? response.json() : null; })
      .then(function (data) {
        if (!data) { hint.textContent = ''; return; }
        hint.textContent = data.available
          ? '当前可用库存：' + data.stock
          : '库存不足，当前可用：' + data.stock;
      })
      .catch(function () { hint.textContent = ''; });
  }

  if (skuInput) {
    skuInput.addEventListener('blur', checkStock);
    if (qtyInput) { qtyInput.addEventListener('change', checkStock); }
  }
})();
