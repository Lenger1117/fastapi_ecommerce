from decimal import Decimal
from fastapi import APIRouter, Depends, HTTPException, status, Response
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.auth import get_current_user
from app.db_depends import get_async_db
from app.models.cart_items import CartItem as CartItemModel
from app.models.products import Product as ProductModel
from app.models.users import User as UserModel
from app.schemas import (
    Cart as CartSchema,
    CartItem as CartItemSchema,
    CartItemCreate,
    CartItemUpdate,
)


router = APIRouter(prefix="/carts", tags=["carts"])


async def _ensure_product_available(product_id: int, db: AsyncSession) -> None:
    result = await db.scalars(select(ProductModel).where(ProductModel.id == product_id, ProductModel.is_active == True,))
    product = result.first()
    if product is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Продукт не найден или неактивен")
    

async def _get_cart_item(user_id: int, product_id: int, db: AsyncSession) -> CartItemModel | None:
    result = await db.scalars(select(CartItemModel)
                              .options(selectinload(CartItemModel.product))
                              .where(CartItemModel.user_id == user_id, CartItemModel.product_id == product_id,)
                              )
    return result.first()


@router.get("/", response_model=CartSchema)
async def get_carts(db: AsyncSession = Depends(get_async_db), current_user: UserModel = Depends(get_current_user),):
    result = await db.scalars(select(CartItemModel)
                              .options(selectinload(CartItemModel.product))
                              .where(CartItemModel.user_id == current_user.id)
                              .order_by(CartItemModel.id)
                              )
    items = result.all()

    total_quantity = sum(item.quantity for item in items)
    price_items = (
        Decimal(item.quantity) *
        (item.product.price if item.product.price is not None else Decimal("0"))
        for item in items
    )
    total_price_decimal = sum(price_items, Decimal("0"))

    return CartSchema(
        user_id=current_user.id,
        items=items,
        total_quantity=total_quantity,
        total_price=total_price_decimal
    )


@router.post("/items", response_model=CartItemSchema, status_code=status.HTTP_201_CREATED)
async def add_item_to_cart(payload: CartItemCreate, current_user: UserModel = Depends(get_current_user), db: AsyncSession = Depends(get_async_db)):
    await _ensure_product_available(payload.product_id, db)

    cart_item = await _get_cart_item(current_user.id, payload.product_id, db)
    if cart_item:
        cart_item.quantity += payload.quantity
    else:
        cart_item = CartItemModel(
            user_id=current_user.id,
            product_id=payload.product_id,
            quantity=payload.quantity,
        )
        db.add(cart_item)

    await db.commit()
    updated_item = await _get_cart_item(current_user.id, payload.product_id, db)
    return updated_item


@router.put("/items/{product_id}", response_model=CartItemSchema)
async def update_cart_item(product_id: int, payload: CartItemUpdate, current_user: UserModel = Depends(get_current_user),
                           db: AsyncSession = Depends(get_async_db)):
    await _ensure_product_available(product_id, db)

    cart_item = await _get_cart_item(current_user.id, product_id, db)
    if cart_item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Корзина не найдена")
    cart_item.quantity = payload.quantity
    await db.commit()
    updated_item = await _get_cart_item(current_user.id, product_id, db)
    return updated_item


@router.delete("/items/{product_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_item_for_cart(product_id: int, current_user: UserModel = Depends(get_current_user), db: AsyncSession = Depends(get_async_db)):
    cart_item = await _get_cart_item(current_user.id, product_id, db)
    if cart_item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Корзина не найдена")
    
    await db.delete(cart_item)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/", status_code=status.HTTP_204_NO_CONTENT)
async def clear_cart(current_user: UserModel = Depends(get_current_user), db: AsyncSession = Depends(get_async_db)):
    await db.execute(delete(CartItemModel).where(CartItemModel.user_id == current_user.id))
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)