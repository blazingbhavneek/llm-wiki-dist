import i18n from 'i18next'
import { initReactI18next } from 'react-i18next'

const resources = {
  ja: {
    translation: {
      appTitle: 'ドキュメントパーサー',
      tagline: 'ファイルをアップロードすると Markdown に変換します',
      dropHint: 'ドラッグ＆ドロップ、またはクリックして選択',
      dropHintFile: '{{name}}',
      formats: 'PDF / DOCX / PPTX / XLSX / CSV',
      includeImages: '画像を含める',
      describeImages: '画像を LLM で説明',
      parse: '解析する',
      reparse: '再解析',
      parsing: '解析中…',
      viewRendered: 'プレビュー',
      viewRaw: 'RAW',
      download: 'Markdown をダウンロード',
      error: 'エラー: {{message}}',
      parsingFile: 'ファイルを解析しています…',
      previewEmpty: 'プレビューはここに表示されます',
      noResult: '変換結果はありません',
      parser: 'パーサー: {{name}}',
      images: '画像: {{count}} 件',
      duration: '所要時間: {{seconds}} 秒',
    },
  },
  en: {
    translation: {
      appTitle: 'Doc Parser',
      tagline: 'Upload a document to convert it to Markdown',
      dropHint: 'Drag & drop, or click to choose a file',
      dropHintFile: '{{name}}',
      formats: 'PDF / DOCX / PPTX / XLSX / CSV',
      includeImages: 'Include images',
      describeImages: 'Describe images with LLM',
      parse: 'Parse',
      reparse: 'Re-parse',
      parsing: 'Parsing…',
      viewRendered: 'Preview',
      viewRaw: 'RAW',
      download: 'Download Markdown',
      error: 'Error: {{message}}',
      parsingFile: 'Parsing your file…',
      previewEmpty: 'Preview will appear here',
      noResult: 'No conversion result',
      parser: 'Parser: {{name}}',
      images: 'Images: {{count}}',
      duration: 'Duration: {{seconds}}s',
    },
  },
}

i18n.use(initReactI18next).init({
  resources,
  lng: localStorage.getItem('lang') || 'ja',
  fallbackLng: 'ja',
  interpolation: { escapeValue: false },
})

i18n.on('languageChanged', (lng) => localStorage.setItem('lang', lng))

export default i18n
