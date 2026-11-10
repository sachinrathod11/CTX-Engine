import { useEffect, useState } from 'react'
import { fetchProducts } from '../api/products'

export default function ProductsPage({ user }) {
  const [items, setItems] = useState([])
  const [query, setQuery] = useState('')

  useEffect(() => {
    fetchProducts(query).then(setItems)
  }, [query])

  return (
    <div>
      <input value={query} onChange={e => setQuery(e.target.value)} placeholder="Search products" />
      <ul>{items.map(p => <li key={p.id}>{p.name} — ₹{p.price / 100}</li>)}</ul>
    </div>
  )
}
