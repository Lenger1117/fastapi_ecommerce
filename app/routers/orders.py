from decimal import Decimal
from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy import select, delete, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.auth import get_current_user
from app.db_depends import get_async_db
from app.models.cart_items import CartItem as CartItemModel
from app.models.orders import Order as OrderModel, OrderItem as OrderItemModel
from app.models.users import User as UserModel
from app.schemas import Order as OrderSchema, OrderList


router = APIRouter(prefix="/orders", tags=["orders"])


async def _load_order_with_items(order_id: int, sb: AsyncSession) -> OrderModel | None:
    result = await sb.scalars(select(OrderModel)
                              .options(selectinload(OrderModel.items).selectinload(OrderItemModel.product),)
                              .where(OrderModel.id == order_id)
                              )
    return result.first()


@router.post("/checkout", response_model=OrderSchema, status_code=status.HTTP_201_CREATED)
async def checkout_order(current_user: UserModel = Depends(get_current_user), db: AsyncSession = Depends(get_async_db)):
    """
    Создает заказ на основе текущей корзины пользователя.
    Сохраняет позиции заказа, вычитает остатки и очищает корзину.
    """
    cart_result = await db.scalars(select(CartItemModel)
                                   .options(selectinload(CartItemModel.product))
                                   .where(CartItemModel.user_id == current_user.id)
                                   .order_by(CartItemModel.id)
                                   )
    cart_items = cart_result.all()
    if cart_items is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Корзина пуста")
    
    order = OrderModel(user_id=current_user.id)
    total_amount = Decimal("0")

    for cart_item in cart_items:
        product = cart_item.product
        if product is None or product.is_active is None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Продукт {cart_item.product_id} недоступен")
        if product.stock < cart_item.quantity:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Недостаточно товара на складе {product.name}")
        
        unit_price = product.price
        if unit_price is None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Цена на товар {product.name} не установлена")
        
        total_price = unit_price * cart_item.quantity
        total_amount += total_price

        order_item = OrderItemModel(
            product_id = cart_item.product_id,
            quantity = cart_item.quantity,
            unit_price = unit_price,
            total_price = total_price,
        )
        order.items.append(order_item)
        product.stock -= cart_item.quantity

    order.total_amount = total_amount
    db.add(order)

    await db.execute(delete(CartItemModel).where(CartItemModel.user_id == current_user.id))
    await db.commit()

    created_order = await _load_order_with_items(order.id, db)
    if created_order is None:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Не удалось загрузить созданный заказ")
    
    return created_order


@router.get("/", response_model=OrderList)
async def list_orders(
    current_user: UserModel = Depends(get_current_user),
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
    db: AsyncSession = Depends(get_async_db),
):
    """
    Возвращает заказы текущего пользователя с простой пагинацией.
    """
    total = await db.scalar(select(func.count(OrderModel.id))
                            .where(OrderModel.user_id == current_user.id)
                            )
    result = await db.scalars(
        select(OrderModel)
        .options(selectinload(OrderModel.items).selectinload(OrderItemModel.product))
        .where(OrderModel.user_id == current_user.id)
        .order_by(OrderModel.created_at.desc())
        .offset((page-1) * page_size)
        .limit(page_size)
    )
    orders = result.all()
    return OrderList(items=orders, total=total or 0, page=page, page_size=page_size)


@router.get("/{order_id}", response_model=OrderSchema)
async def get_order(order_id: int,
                    current_user: UserModel = Depends(get_current_user),
                    db: AsyncSession = Depends(get_async_db)):
    """
    Возвращает детальную информацию по заказуб если он пренадлежит пользователю.
    """
    order = await _load_order_with_items(order_id, db)
    if order is None or order.user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Заказ не найден")
    return order