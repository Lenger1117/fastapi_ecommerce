from pathlib import Path
import uuid
from fastapi import UploadFile, File, Form

from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy import select, update, func, desc, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.products import Product as ProductModel
from app.models.categories import Category as CategoryModel
from app.models.users import User as UserModel
from app.schemas import Product as ProductSchema, ProductCreate, ProductList
from app.db_depends import get_async_db
from app.auth import get_current_seller


BASE_DIR = Path(__file__).resolve().parent.parent.parent
MEDIA_ROOT = BASE_DIR / "media" / "products"
MEDIA_ROOT.mkdir(parents=True, exist_ok=True)
ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}
MAX_IMAGE_SIZE = 2 * 1024 * 1024

router = APIRouter(
    prefix="/products",
    tags=["products"],
)


async def save_product_image(file: UploadFile) -> str:
    """
    Сохраняет изображение товара и возвращает относительный URL.
    """
    if file.content_type not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Разрешены только изображения в формате JPG, PNG или WebP")
    
    content = await file.read()
    if len(content) > MAX_IMAGE_SIZE:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Размер изображения слишком большой")
    
    extension = Path(file.filename or "").suffix.lower() or ".jpg"
    file_name = f"{uuid.uuid4()}{extension}"
    file_path = MEDIA_ROOT / file_name
    file_path.write_bytes(content)

    return f"/media/products/{file_name}"


def remove_product_image(url: str | None) -> None:
    """
    Удаляет файл изображения, если он существует.
    """
    if not url:
        return
    
    relative_path = url.lstrip("/")
    file_path = BASE_DIR / relative_path
    if file_path.exists():
        file_path.unlink()


@router.get("/", response_model=ProductList)
async def get_all_products(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    category_id: int | None = Query(None, description="ID категории для фильтрации"),
    min_price: float | None = Query(None, ge=0, description="Минимальная цена товара"),
    max_price: float | None = Query(None, ge=0, description="Максимальная цена товара"),
    in_stock: bool | None = Query(None, description="true — только товары в наличии, false — только без остатка"),
    seller_id: int | None = Query(None, description="ID продавца для фильтрации"),
    search: str | None = Query(None, min_length=1, description="Поиск по названию товара"),
    db: AsyncSession = Depends(get_async_db)
    ):
    """
    Возвращает список всех товаров.
    """
    # Проверка логики min_price <= max_price
    if min_price is not None and max_price is not None and min_price > max_price:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="min_price не может быть больше max_price")
    
    # Формируем список фильтров
    filters = [ProductModel.is_active == True]

    if category_id is not None:
        filters.append(ProductModel.category_id == category_id)
    if min_price is not None:
        filters.append(ProductModel.price >= min_price)
    if max_price is not None:
        filters.append(ProductModel.price <= max_price)
    if in_stock is not None:
        filters.append(ProductModel.stock > 0 if in_stock else ProductModel.stock == 0)
    if seller_id is not None:
        filters.append(ProductModel.seller_id == seller_id)

    total_stmt = select(func.count()).select_from(ProductModel).where(*filters)

    rank_col = None
    if search:
        search_value = search.strip()
        if search_value:
            ts_query = func.websearch_to_tsquery('english', search_value)
            filters.append(ProductModel.tsv.op('@@')(ts_query))
            rank_col = func.ts_rank_cd(ProductModel.tsv, ts_query).label("rank")
            # total с учетом полнотекстного фильтра
            total_stmt = select(func.count()).select_from(ProductModel).where(*filters)

    total = await db.scalar(total_stmt) or 0

    # Основной запрос (если есть поиск - добавим ранг в выборку и сортировку)
    if rank_col is not None:
        product_stmt = (
            select(ProductModel, rank_col)
            .where(*filters)
            .order_by(desc(rank_col), ProductModel.id)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        result = await db.execute(product_stmt)
        rows = result.all()
        items = [row[0] for row in rows]
    else:
        product_stmt = (
            select(ProductModel)
            .where(*filters)
            .order_by(ProductModel.id)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        items = (await db.scalars(product_stmt)).all()
        
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }

@router.post("/", response_model=ProductSchema, status_code=status.HTTP_201_CREATED)
async def create_product(
    product: ProductCreate = Depends(ProductCreate.as_form),
    image: UploadFile | None = File(None),
    db: AsyncSession = Depends(get_async_db),
    current_user: UserModel = Depends(get_current_seller)):
    """
    Создаёт новый товар, привязанный к текущему продавцу (только для 'seller').
    """
    if product.category_id is not None:
        result = await db.scalars(select(CategoryModel).where(CategoryModel.id == product.category_id,
                                           CategoryModel.is_active == True))
        category = result.first()
        if category is None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Категория не найдена или неактивна.")
        
    # Сохранение изображения (если есть)
    image_url = await save_product_image(image) if image else None
        
    db_product = ProductModel(**product.model_dump(), seller_id=current_user.id, image_url=image_url)
    db.add(db_product)
    await db.commit()
    await db.refresh(db_product)
    return db_product

@router.get("/category/{category_id}", response_model=list[ProductSchema])
async def get_products_by_category(category_id: int, db: AsyncSession = Depends(get_async_db)):
    """
    Возвращает список товаров в указанной категории по её ID.
    """
    result_category = await db.scalars(select(CategoryModel).where(CategoryModel.id == category_id,
                                                CategoryModel.is_active == True))
    db_category = result_category.first()
    if db_category is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Категория не найдена или неактивна")
    
    result_product = await db.scalars(select(ProductModel).where(
        ProductModel.category_id == category_id,
        ProductModel.is_active == True,
        ProductModel.stock > 0
    ))
    products = result_product.all()
    return products

@router.get("/{product_id}", response_model=ProductSchema)
async def get_product(product_id: int, db: AsyncSession = Depends(get_async_db)):
    """
    Возвращает детальную информацию о товаре по его ID.
    """
    result_product = await db.scalars(select(ProductModel).where(ProductModel.id == product_id, ProductModel.is_active == True, ProductModel.stock > 0))
    db_product = result_product.first()
    if db_product is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Продукт не найден")
    
    result_category = await db.scalars(select(CategoryModel).where(CategoryModel.id == db_product.category_id,
                                                CategoryModel.is_active == True))
    db_category = result_category.first()
    if db_category is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Категория не найдена")
    
    return db_product

@router.put("/{product_id}", response_model=ProductSchema)
async def update_product(
    product_id: int,
    product: ProductCreate = Depends(ProductCreate.as_form),
    image: UploadFile | None = File(None),
    db: AsyncSession = Depends(get_async_db),
    current_user: UserModel = Depends(get_current_seller)):
    """
    Обновляет товар, если он принадлежит текущему продавцу (только для 'seller').
    """
    # Проверка существования продукта
    result_product = await db.scalars(select(ProductModel).where(ProductModel.id == product_id, ProductModel.is_active == True))
    db_product = result_product.first()
    if db_product is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Продукт не найден")
    if db_product.seller_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Вы можете обновлять только свои собственные продукты")
    
    # Проверка существования категории
    result_category = await db.scalars(select(CategoryModel).where(CategoryModel.id == product.category_id, CategoryModel.is_active == True))
    db_category = result_category.first()
    if db_category is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Категория не найдена")
    
    # Обновление продукта
    await db.execute(
        update(ProductModel)
        .where(ProductModel.id == product_id)
        .values(**product.model_dump())
    )

    if image:
        remove_product_image(db_product.image_url)
        db_product.image_url = await save_product_image(image)

    await db.commit()
    await db.refresh(db_product)
    return db_product

@router.delete("/{product_id}", status_code=status.HTTP_200_OK)
async def delete_product(product_id: int,
                         db: AsyncSession = Depends(get_async_db),
                         current_user: UserModel = Depends(get_current_seller)):
    """
    Выполняет мягкое удаление товара, если он принадлежит текущему продавцу (только для 'seller').
    """
    # Проверка существования активного товара.
    result = await db.scalars(select(ProductModel). where(ProductModel.id == product_id, ProductModel.is_active == True))
    product = result.first()
    if product is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Продукт не найден")
    if product.seller_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Вы можете удалить только свои собственные продукты")
    
    # Проверяем, существует ли активная категория
    category_result = await db.scalars(
        select(CategoryModel).where(CategoryModel.id == product.category_id,
                                    CategoryModel.is_active == True)
    )
    category = category_result.first()
    if not category:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="Категория не найдена или неактивна")
    
    # Логическое удаление продукта (установка is_active=False)
    await db.execute(
        update(ProductModel)
        .where(ProductModel.id == product_id)
        .values(is_active=False)
        )
    
    remove_product_image(product.image_url)
    
    await db.commit()
    await db.refresh(product)
    return product