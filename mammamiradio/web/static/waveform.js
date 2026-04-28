/*
 * Mamma Mi Radio — Canonical Waveform
 * One component, two variants, four states. Used on all surfaces.
 *
 * Usage:
 *   <div class="waveform" data-variant="strip"></div>
 *   <div class="waveform" data-variant="hero"></div>
 *
 * Auto-initializes on DOMContentLoaded for any empty .waveform element.
 * Exposes globals: initWaveform, setWaveformPaused, setWaveformVariant.
 *
 * Spec: docs/design/system.md § "Waveform — canonical, two variants, four states"
 */

(function () {
  'use strict';

  var BAR_COUNT = { hero: 24, strip: 36 };
  var MAX_HEIGHT = { hero: 56, strip: 22 };

  function initWaveform(el) {
    var variant = el.dataset.variant === 'hero' ? 'hero' : 'strip';
    el.classList.add('waveform', variant);
    el.innerHTML = '';
    var count = BAR_COUNT[variant];
    var maxH = MAX_HEIGHT[variant];
    for (var i = 0; i < count; i++) {
      var bar = document.createElement('div');
      bar.className = 'waveform-bar';
      var h = 6 + Math.random() * (maxH - 6);
      var d = (0.45 + Math.random() * 0.85).toFixed(2);
      var dl = (Math.random() * 0.7).toFixed(2);
      bar.style.setProperty('--h', h + 'px');
      bar.style.setProperty('--d', d + 's');
      bar.style.setProperty('--dl', dl + 's');
      el.appendChild(bar);
    }
  }

  function setWaveformPaused(el, paused) {
    el.classList.toggle('paused', paused);
  }

  function setWaveformVariant(el, variant) {
    if (variant !== 'hero' && variant !== 'strip') return;
    el.classList.remove('hero', 'strip');
    el.classList.add(variant);
    el.dataset.variant = variant;
    initWaveform(el);
  }

  window.initWaveform = initWaveform;
  window.setWaveformPaused = setWaveformPaused;
  window.setWaveformVariant = setWaveformVariant;

  function autoInit() {
    var nodes = document.querySelectorAll('.waveform');
    for (var i = 0; i < nodes.length; i++) {
      if (nodes[i].children.length === 0) {
        initWaveform(nodes[i]);
      }
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', autoInit);
  } else {
    autoInit();
  }
})();
