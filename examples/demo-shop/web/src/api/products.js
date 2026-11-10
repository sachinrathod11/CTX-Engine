import axios from 'axios'

export async function fetchProducts(query = '') {
  const { data } = await axios.get('/api/products', { params: { q: query } })
  return data
}

export async function placeOrder(items) {
  const { data } = await axios.post('/api/orders', { items }, { withCredentials: true })
  return data
}
