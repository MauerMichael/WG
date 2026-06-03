/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    './app/templates/**/*.html',
    './app/static/js/**/*.js',
  ],
  theme: {
    extend: {
      colors: {
        // Marke + Primaer-Aktion (Violett)
        brand: {
          50: '#f5f3ff',
          100: '#ede9fe',
          200: '#ddd6fe',
          300: '#c4b5fd',
          400: '#a78bfa',
          500: '#8b5cf6',
          600: '#7c3aed',
          700: '#6d28d9',
          800: '#5b21b6',
          900: '#4c1d95',
        },
        // Belohnung + positive Highlights (Gold) -- waermer/edler als das alte Neon-Gelb
        gold: {
          50: '#fffbeb',
          100: '#fef3c7',
          200: '#fde9a8',
          300: '#fbd96b',
          400: '#f5c518',
          500: '#e5a800',
          600: '#c68a00',
          700: '#9a6a00',
          800: '#7a5400',
          900: '#5c3f00',
        },
        // Sanftes Lila-Weiss als Seiten-Hintergrund
        surface: '#f7f5fb',
      },
      fontFamily: {
        sans: ['Nunito', 'system-ui', '-apple-system', 'Segoe UI', 'Roboto', 'Helvetica', 'Arial', 'sans-serif'],
        // Runde, freundliche Anzeige-Schrift fuer Ueberschriften (Soft-Rounded-Pairing)
        display: ['"Varela Round"', 'Nunito', 'system-ui', 'sans-serif'],
      },
      boxShadow: {
        // Violett-getoenter, weicher Schatten -- Kern des "gemuetlichen" Looks
        soft: '0 4px 24px -6px rgba(124, 58, 237, 0.15)',
        'soft-lg': '0 12px 40px -8px rgba(124, 58, 237, 0.20)',
      },
    },
  },
  plugins: [],
};
