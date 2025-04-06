document.addEventListener('DOMContentLoaded', () => {
    const likeButtons = document.querySelectorAll('.like-btn');

    likeButtons.forEach(button => {
        button.addEventListener('click', async () => {
            const newsId = button.getAttribute('data-news-id');
            const isLiked = button.getAttribute('data-liked') === '1';

            try {
                const response = await fetch(`/like/${newsId}`, {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                    },
                });

                if (!response.ok) {
                    const errorData = await response.json();
                    alert(errorData.error || 'Произошла ошибка.');
                    return;
                }

                const data = await response.json();
                const likeCountElement = button.querySelector('.like-count');

                // Обновляем состояние кнопки и счетчик лайков
                button.setAttribute('data-liked', data.action === 'liked' ? '1' : '0');
                likeCountElement.textContent = data.like_count;

                // Меняем цвет иконки
                const likeIcon = button.querySelector('.like-icon');
                likeIcon.style.fill = data.action === 'liked' ? 'red' : 'black';
            } catch (error) {
                console.error('Ошибка при обработке лайка:', error);
                alert('Произошла ошибка. Попробуйте снова.');
            }
        });
    });
});