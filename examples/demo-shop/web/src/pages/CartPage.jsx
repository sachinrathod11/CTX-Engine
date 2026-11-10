import { useState } from 'react'
import { placeOrder } from '../api/products'

export default function CartPage({ user }) {
  const [cart] = useState([{ id: 1, qty: 1 }])
  const [status, setStatus] = useState('')

  async function checkout() {
    if (!user) return setStatus('Please log in first')
    const order = await placeOrder(cart)
    setStatus(`Order #${order.id} placed`)
  }

  return <button onClick={checkout}>Checkout</button>
}
