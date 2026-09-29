/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        ink: '#172033',
        muted: '#667085',
        primary: '#7C3AED',
        sidebar: '#151B2C',
      },
    },
  },
  plugins: [],
}

