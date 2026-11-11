from django.urls import path

from .consumers import ProjectBoardConsumer, TaskChatConsumer

websocket_urlpatterns = [
    path("ws/tasks/<int:task_id>/", TaskChatConsumer.as_asgi()),
    path("ws/projects/<int:project_id>/", ProjectBoardConsumer.as_asgi()),
]
