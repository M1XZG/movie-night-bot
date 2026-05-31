#!/usr/bin/env python3
"""Generate the Movie Night bot avatar (popcorn + movie camera) as a PNG."""
import cairosvg

SVG = '''<?xml version="1.0" encoding="UTF-8"?>
<svg width="512" height="512" viewBox="0 0 512 512" xmlns="http://www.w3.org/2000/svg">
  <defs>
    <radialGradient id="bg" cx="50%" cy="38%" r="75%">
      <stop offset="0%" stop-color="#3a2150"/>
      <stop offset="55%" stop-color="#241038"/>
      <stop offset="100%" stop-color="#140821"/>
    </radialGradient>
    <linearGradient id="gold" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="#ffe28a"/>
      <stop offset="100%" stop-color="#f5b524"/>
    </linearGradient>
    <linearGradient id="cam" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="#4b5563"/>
      <stop offset="100%" stop-color="#1f2530"/>
    </linearGradient>
    <linearGradient id="bucket" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="#ff5d54"/>
      <stop offset="100%" stop-color="#c62f30"/>
    </linearGradient>
    <radialGradient id="spot" cx="50%" cy="30%" r="60%">
      <stop offset="0%" stop-color="#fff4d6" stop-opacity="0.20"/>
      <stop offset="100%" stop-color="#fff4d6" stop-opacity="0"/>
    </radialGradient>
    <filter id="sh" x="-30%" y="-30%" width="160%" height="160%">
      <feDropShadow dx="0" dy="6" stdDeviation="8" flood-color="#000" flood-opacity="0.45"/>
    </filter>
  </defs>

  <!-- background -->
  <rect width="512" height="512" rx="110" fill="url(#bg)"/>
  <rect width="512" height="512" rx="110" fill="url(#spot)"/>

  <!-- film-strip arc across the top -->
  <g opacity="0.9">
    <path d="M40 132 Q256 60 472 132 L472 176 Q256 104 40 176 Z" fill="#0d0612"/>
    <g fill="#f5b524">
      <rect x="64" y="120" width="22" height="16" rx="3"/>
      <rect x="120" y="108" width="22" height="16" rx="3"/>
      <rect x="178" y="100" width="22" height="16" rx="3"/>
      <rect x="236" y="96"  width="22" height="16" rx="3"/>
      <rect x="294" y="100" width="22" height="16" rx="3"/>
      <rect x="352" y="108" width="22" height="16" rx="3"/>
      <rect x="408" y="120" width="22" height="16" rx="3"/>
    </g>
  </g>

  <!-- ===== Movie camera (left) ===== -->
  <g filter="url(#sh)" transform="translate(96 226) rotate(-6)">
    <!-- film reels on top -->
    <g>
      <circle cx="46" cy="-26" r="34" fill="url(#cam)" stroke="#0b0f16" stroke-width="3"/>
      <circle cx="46" cy="-26" r="9" fill="#9aa6b4"/>
      <g fill="#0b0f16"><circle cx="46" cy="-46" r="5"/><circle cx="62" cy="-20" r="5"/><circle cx="30" cy="-20" r="5"/><circle cx="58" cy="-40" r="4"/><circle cx="34" cy="-40" r="4"/></g>
      <circle cx="120" cy="-26" r="34" fill="url(#cam)" stroke="#0b0f16" stroke-width="3"/>
      <circle cx="120" cy="-26" r="9" fill="#9aa6b4"/>
      <g fill="#0b0f16"><circle cx="120" cy="-46" r="5"/><circle cx="136" cy="-20" r="5"/><circle cx="104" cy="-20" r="5"/><circle cx="132" cy="-40" r="4"/><circle cx="108" cy="-40" r="4"/></g>
    </g>
    <!-- body -->
    <rect x="6" y="2" width="150" height="96" rx="14" fill="url(#cam)" stroke="#0b0f16" stroke-width="3"/>
    <!-- lens -->
    <rect x="150" y="30" width="44" height="40" rx="6" fill="#2b323d" stroke="#0b0f16" stroke-width="3"/>
    <circle cx="196" cy="50" r="22" fill="#11151c" stroke="#5b6675" stroke-width="4"/>
    <circle cx="196" cy="50" r="11" fill="#0a0d12"/>
    <circle cx="190" cy="44" r="4" fill="#9fd2ff" opacity="0.85"/>
    <!-- crank handle -->
    <rect x="-12" y="44" width="22" height="10" rx="5" fill="#2b323d" stroke="#0b0f16" stroke-width="2"/>
    <circle cx="-14" cy="49" r="9" fill="#f5b524" stroke="#0b0f16" stroke-width="2"/>
    <!-- accent line -->
    <rect x="18" y="74" width="120" height="8" rx="4" fill="#f5b524" opacity="0.85"/>
  </g>

  <!-- ===== Popcorn bucket (right) ===== -->
  <g filter="url(#sh)" transform="translate(300 214)">
    <!-- popcorn pile -->
    <g fill="#fff3d4" stroke="#e9c873" stroke-width="2">
      <circle cx="40" cy="6"  r="17"/>
      <circle cx="68" cy="-6" r="19"/>
      <circle cx="96" cy="2"  r="17"/>
      <circle cx="54" cy="-14" r="15"/>
      <circle cx="82" cy="-18" r="16"/>
      <circle cx="28" cy="-8" r="14"/>
      <circle cx="108" cy="-12" r="14"/>
      <circle cx="68" cy="-30" r="15"/>
    </g>
    <!-- a couple of flying kernels -->
    <circle cx="120" cy="-44" r="11" fill="#fff3d4" stroke="#e9c873" stroke-width="2"/>
    <circle cx="16"  cy="-34" r="9"  fill="#fff3d4" stroke="#e9c873" stroke-width="2"/>
    <!-- bucket (trapezoid) -->
    <path d="M22 18 L118 18 L132 150 L8 150 Z" fill="url(#bucket)" stroke="#7d1d1d" stroke-width="3"/>
    <!-- white stripes -->
    <path d="M40 18 L46 150 L28 150 L24 18 Z" fill="#fff" opacity="0.92"/>
    <path d="M74 18 L78 150 L60 150 L58 18 Z" fill="#fff" opacity="0.92"/>
    <path d="M108 18 L120 150 L102 150 L92 18 Z" fill="#fff" opacity="0.92"/>
    <!-- rim -->
    <rect x="16" y="12" width="108" height="14" rx="7" fill="#ff7a73" stroke="#7d1d1d" stroke-width="3"/>
  </g>
</svg>'''

cairosvg.svg2png(bytestring=SVG.encode("utf-8"),
                 write_to=str(__import__("pathlib").Path(__file__).with_name("avatar.png")),
                 output_width=512, output_height=512)
print("wrote avatar.png")
