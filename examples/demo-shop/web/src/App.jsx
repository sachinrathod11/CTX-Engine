import { Routes, Route } from 'react-router-dom'
import LoginForm from './components/LoginForm'
import ProductsPage from './pages/ProductsPage'
import CartPage from './pages/CartPage'
import { useAuth } from './hooks/useAuth'

export default function App() {
  const { user, setUser } = useAuth()
  return (
    <Routes>
      <Route path="/" element={<ProductsPage user={user} />} />
      <Route path="/login" element={<LoginForm onLoggedIn={setUser} />} />
      <Route path="/cart" element={<CartPage user={user} />} />
    </Routes>
  )
}
