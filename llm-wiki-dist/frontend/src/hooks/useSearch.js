import { useRef, useState } from 'react'

import { api } from '../api'

/**
 * Keyword search state for the top bar.
 *
 * Important:
 * - Does not run the agent.
 * - Does not auto-open the first result.
 * - Returns multiple nodes to TopBar/SearchResultsCenter.
 */
export function useSearch({ t, fireToast, setCenterView }) {
  const [searchQuery, setSearchQuery] = useState('')
  const [searchResults, setSearchResults] = useState([])
  const [searchLoading, setSearchLoading] = useState(false)
  const searchSeq = useRef(0)

  const onSearch = async (text) => {
    const clean = text.trim()

    if (!clean) return []

    searchSeq.current += 1

    const seq = searchSeq.current

    setSearchQuery(clean)
    setSearchResults([])
    setSearchLoading(true)
    setCenterView('search')

    try {
      const results = await api.search(clean, 12)
      const list = Array.isArray(results) ? results : []

      if (seq === searchSeq.current) {
        setSearchResults(list)
      }

      return list
    } catch (e) {
      if (seq === searchSeq.current) {
        setSearchResults([])
      }

      fireToast(t.searchFailed(e.message))

      return []
    } finally {
      if (seq === searchSeq.current) {
        setSearchLoading(false)
      }
    }
  }

  /** Show externally produced results (TopBar already ran the query). */
  const showResults = ({ query, results }) => {
    setSearchQuery(query)
    setSearchResults(Array.isArray(results) ? results : [])
    setSearchLoading(false)
    setCenterView('search')
  }

  return { searchQuery, searchResults, searchLoading, onSearch, showResults }
}
