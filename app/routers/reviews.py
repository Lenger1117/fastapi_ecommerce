from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select, func, update
from sqlalchemy.ext.asyncio import AsyncSession
from decimal import Decimal
from datetime import datetime

from app.schemas import ReviewCreate, Review as ReviewSchema
from app.models.reviews import Review
from app.models.products import Product
from app.models.users import User
from app.db_depends import get_async_db
from app.auth import get_current_user

router = APIRouter(prefix="/reviews", tags=["reviews"])


@router.get("/", response_model=list[ReviewSchema])
async def get_all_reviews(db: AsyncSession = Depends(get_async_db)):
    result = await db.scalars(
        select(Review).where(Review.is_active.is_(True))
    )
    reviews = result.all()
    return reviews


@router.get("/products/{product_id}/reviews", response_model=list[ReviewSchema])
async def get_reviews_for_product(product_id: int, db: AsyncSession = Depends(get_async_db)):
    # Проверяем, существует ли товар и активен ли он
    product_result = await db.scalars(
        select(Product).where(Product.id == product_id, Product.is_active.is_(True))
    )
    product = product_result.first()
    if not product:
        raise HTTPException(status_code=404, detail="Продукт не найден или неактивен")

    # Получаем активные отзывы для товара
    reviews_result = await db.scalars(
        select(Review).where(
            Review.product_id == product_id,
            Review.is_active.is_(True)
        )
    )
    reviews = reviews_result.all()
    return reviews


async def recalculate_product_rating(product_id: int, db: AsyncSession):
    # Считаем среднюю оценку по активным отзывам
    avg_result = await db.scalars(
        select(func.avg(Review.grade)).where(
            Review.product_id == product_id,
            Review.is_active.is_(True)
        )
    )
    avg_grade = avg_result.first()

    # Обновляем rating в товаре (может быть NULL, если отзывов нет)
    new_rating = Decimal(str(round(avg_grade, 2))) if avg_grade is not None else None

    await db.execute(
        update(Product)
        .where(Product.id == product_id)
        .values(rating=new_rating)
    )


@router.post("/", response_model=ReviewSchema, status_code=status.HTTP_201_CREATED)
async def create_review(
    review_data: ReviewCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db)
):
    # Проверка роли: только "buyer"
    if current_user.role != "buyer":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Отзывы могут оставлять только покупатели"
        )

    # Проверяем, существует ли товар и активен ли он
    product_result = await db.scalars(
        select(Product).where(
            Product.id == review_data.product_id,
            Product.is_active.is_(True)
        )
    )
    product = product_result.first()
    if not product:
        raise HTTPException(status_code=404, detail="Продукт не найден или неактивен")

    # Создаём отзыв
    new_review = Review(
        user_id=current_user.id,
        product_id=review_data.product_id,
        comment=review_data.comment,
        grade=review_data.grade,
        comment_date=datetime.now(),
        is_active=True
    )
    db.add(new_review)

    # Пересчитываем рейтинг
    await recalculate_product_rating(review_data.product_id, db)

    await db.commit()
    await db.refresh(new_review)
    return new_review


@router.delete("/{review_id}")
async def delete_review(
    review_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db)
):
    if current_user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Удалять отзывы могут только администраторы"
        )

    # Находим отзыв
    review_result = await db.scalars(
        select(Review).where(Review.id == review_id, Review.is_active.is_(True))
    )
    review = review_result.first()
    if not review:
        raise HTTPException(status_code=404, detail="Отзыв не найден или удален")

    # Мягкое удаление
    review.is_active = False

    # Пересчитываем рейтинг товара
    await recalculate_product_rating(review.product_id, db)

    await db.commit()
    return {"message": "Review deleted"}