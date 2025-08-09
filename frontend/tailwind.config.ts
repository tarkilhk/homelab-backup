import type { Config } from 'tailwindcss'

export default {
  content: ['./index.html', './src/**/*.{ts,tsx,css}'],
  theme: {
    extend: {},
    container: {
      center: true,
      padding: '1rem',
    },
  },
  plugins: [require('tailwindcss-animate')],
  future: {
    hoverOnlyWhenSupported: true,
  },
} satisfies Config


