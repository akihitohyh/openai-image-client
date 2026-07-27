/** @type {import('tailwindcss').Config} */
module.exports = {
  content: ['./web/index.html', './web/app.js'],
  theme: {
    extend: {
      colors: {
        // 深色工作台的层次：页面 → 面板 → 控件 → 悬停
        ink: {
          950: '#0b0d11',
          900: '#101319',
          850: '#161a22',
          800: '#1d222c',
          700: '#272d3a',
        },
        accent: {
          DEFAULT: '#22d3ee',
          soft: '#67e8f9',
          deep: '#0891b2',
        },
      },
      fontFamily: {
        sans: ['Avenir Next', 'Segoe UI', 'Helvetica Neue', 'sans-serif'],
        mono: ['SF Mono', 'JetBrains Mono', 'Menlo', 'Consolas', 'monospace'],
      },
      boxShadow: {
        // 深色下用高光描边代替阴影来表达层次，阴影只用于抬升
        raise: '0 1px 0 0 rgb(255 255 255 / 6%) inset, 0 8px 24px -12px rgb(0 0 0 / 80%)',
        glow: '0 0 0 1px rgb(34 211 238 / 35%), 0 8px 32px -8px rgb(34 211 238 / 25%)',
      },
      keyframes: {
        'fade-in': {
          from: { opacity: '0', transform: 'translateY(4px)' },
          to: { opacity: '1', transform: 'translateY(0)' },
        },
        shimmer: {
          '100%': { transform: 'translateX(100%)' },
        },
      },
      animation: {
        'fade-in': 'fade-in 180ms ease-out',
        shimmer: 'shimmer 1.6s infinite',
      },
    },
  },
  plugins: [],
};
