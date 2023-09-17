import kanboard
import requests
from redminelib import Redmine

# Redmine 설정
REDMINE_URL = ""  # "https://..."
REDMINE_USERNAME = ""  # "metalg0su"
REDMINE_API_KEY = ""  # "..."

# Kanboard 설정
KANBOARD_URL = ""  # "http://localhost/jsonrpc.php"
KANBOARD_USERNAME = "jsonrpc"
KANBOARD_API_KEY = ""  # "..."


class Migrator:
    """
    기본적으로 다음 전략을 취함
        - 있으면 캐싱 후 건너뜀
        - 없으면 생성 후 캐싱
    """
    __redmine: Redmine
    __kanboard: kanboard.Client

    __project_map: dict

    # User cache
    __user_map: dict
    __redmine_users: dict
    __kanboard_users: dict

    def __init__(self):
        self.__redmine = Redmine(REDMINE_URL, key=REDMINE_API_KEY)
        self.__kanboard = kanboard.Client(url=KANBOARD_URL, username=KANBOARD_USERNAME, password=KANBOARD_API_KEY)

        self.__project_map = {}

        self.__user_map = {}  # redmine_user_id -> kanboard_user
        self.__redmine_users = {}
        self.__kanboard_users = {}

        # status
        self.__status_map = {}
        self.__redmine_status = {status.id: status for status in self.__redmine.issue_status.all()}

        trackers = self.__redmine.tracker.all()
        color_ids = self.__kanboard.get_color_list().keys()
        # TODO: 이슈 유형이 기본 컬러보다 넘칠 땐, 사용자에게 병합을 위임해야 할 것임
        assert len(color_ids) >= len(trackers)
        self.__color_map = {tracker.id: color_id for tracker, color_id in zip(trackers, color_ids)}  # tracker: color

    def _grant_user_permission(self, kanboard_project_id: int, redmine_user_id: int):
        self.__kanboard.add_project_user(
            project_id=kanboard_project_id,
            user_id=self.__user_map[redmine_user_id]["id"]
        )

    def _create_projects(self):
        """프로젝트 생성"""

        for project in self.__redmine.project.all():
            project_id = project["id"]
            name = project["name"]
            kb_proj = self.__kanboard.get_project_by_name(name=name)
            kb_proj_id: int
            if kb_proj:
                kb_proj_id = kb_proj["id"]
                self.__project_map[project_id] = kb_proj_id
            else:
                print("프로젝트가 없음. 생성함: ", name)
                kb_proj_id = self.__kanboard.create_project(name=name)
                self.__project_map[project_id] = kb_proj_id

            for membership in project.memberships:
                # TODO: group 무시
                if hasattr(membership, "user"):
                    self._grant_user_permission(kb_proj_id, membership.user.id)

            self._create_categories(project_id, kb_proj_id)

            # kanboard에서 상태는 프로젝트 귀속임
            self._create_columns(project_id, kb_proj_id)

    def _create_columns(self, redmine_project_id: int, kanboard_project_id: int):
        """상태 컬럼 생성

        생성된 후 알아서 순서나, 불용 컬럼은 삭제하는 걸로
        """
        kb_columns = self.__kanboard.get_columns(project_id=kanboard_project_id)
        kb_status_names = set(status["title"] for status in kb_columns)

        for status in self.__redmine_status.values():
            if status.name not in kb_status_names:
                print("상태 컬럼 없음. 생성함: ", status.name)
                _id = self.__kanboard.add_column(
                    project_id=kanboard_project_id,
                    title=status.name,
                )

    def _create_categories(self, redmine_project_id, kanboard_project_id: int):
        kb_categories = self.__kanboard.get_all_categories(project_id=kanboard_project_id)
        kb_cate_names = set(category["name"] for category in kb_categories)

        for issue_category in self.__redmine.issue_category.filter(project_id=redmine_project_id):
            if issue_category.name not in kb_cate_names:
                _id = self.__kanboard.create_category(
                    name=issue_category.name,
                    project_id=kanboard_project_id
                )

    def _create_users(self):
        """유저 생성"""
        users = self.__redmine.user.all()
        for user in users:
            self.__redmine_users[user.id] = user

            kb_username = user.login
            kb_user = self.__kanboard.get_user_by_name(username=kb_username)
            if kb_user:
                self.__kanboard_users[kb_user["id"]] = kb_user
            else:
                print("유저 없음. 생성함: ", kb_username)
                kb_id = self.__kanboard.create_user(
                    username=kb_username,
                    password="123123",
                    name=user.lastname + user.firstname,
                    email=user.mail,
                )
                kb_user = self.__kanboard.get_user_by_name(username=kb_username)
                self.__kanboard_users[kb_id] = kb_user

            self.__user_map[user.id] = kb_user

    def __generate_project_category_map(self, redmine_project_id: int, kanboard_project_id: int):
        """프로젝트 카테고리 매핑 생성

        id -> kanboard_issue_category
        """
        kb_categories = self.__kanboard.get_all_categories(project_id=kanboard_project_id)
        kb_cates_by_name = {category["name"]: category for category in kb_categories}

        redmine_categories = self.__redmine.issue_category.filter(project_id=redmine_project_id)

        return {category.id: kb_cates_by_name[category.name] for category in redmine_categories}

    def _set_relations(self, redmine_project_id: int, kanboard_project_id: int):
        print("===== 작업 간 관계 생성 중 =====")
        kb_links = self.__kanboard.get_all_links()
        kb_links_by_label = {link["label"]: link for link in kb_links}

        tasks = self.__redmine.issue.filter(project_id=redmine_project_id, status_id="*")
        # REDMINE에는 all relations을 가져오는게 없음
        for task in tasks:
            # ==== 관계 생성 (단방향만 해야 함)
            if task.relations:
                for rel in task.relations:
                    relation_name = rel.relation_type

                    if relation_name in ["precedes", "duplicates"]:
                        start_task = self.__kanboard.get_task_by_reference(project_id=kanboard_project_id,
                                                                           reference=rel.issue_id)
                        end_task = self.__kanboard.get_task_by_reference(project_id=kanboard_project_id,
                                                                         reference=rel.issue_to_id)
                        link_id = kb_links_by_label["blocks"]["id"]

                        task_link_id = self.__kanboard.create_task_link(
                            task_id=start_task["id"],
                            opposite_task_id=end_task["id"],
                            link_id=link_id,
                        )
                        print(f"- relation '{relation_name}': {start_task['title']} -> {end_task['title']}")

            # ==== 부모 -> 자식 링크  # redmine에선 부모자식은 별도의 관계임
            if task.children:
                for child in task.children:
                    start_task = self.__kanboard.get_task_by_reference(project_id=kanboard_project_id,
                                                                       reference=task.id)
                    end_task = self.__kanboard.get_task_by_reference(project_id=kanboard_project_id, reference=child.id)
                    link_id = kb_links_by_label["is a parent of"]["id"]

                    task_link_id = self.__kanboard.create_task_link(
                        task_id=start_task["id"],
                        opposite_task_id=end_task["id"],
                        link_id=link_id,
                    )
                    print(f"- relation 'parent-child': {start_task['title']} -> {end_task['title']}")

    def _create_tasks(self, redmine_project_id: int, kanboard_project_id: int):
        print("===== 작업 가져오는 중 =====")
        tasks = self.__redmine.issue.filter(project_id=redmine_project_id, status_id="*")

        # status
        kanboard_status_by_name: dict = {status["title"]: status
                                         for status in self.__kanboard.get_columns(project_id=kanboard_project_id)}

        # category
        category_map = self.__generate_project_category_map(redmine_project_id, kanboard_project_id)

        for task in tasks:
            kb_task = self.__kanboard.get_task_by_reference(project_id=kanboard_project_id, reference=task.id)
            if kb_task:
                continue

            print("작업 생성: ", task.id)
            _id = self.__kanboard.create_task(
                title=task.subject,
                project_id=kanboard_project_id,
                color_id=self.__color_map[task.tracker.id],
                column_id=kanboard_status_by_name[task.status.name]["id"],
                owner_id=self.__user_map[task.assigned_to.id]["id"],
                creator_id=self.__user_map[task.author.id]["id"],
                date_due=task.due_date.strftime("%Y-%m-%d"),
                description=task.description,
                category_id=category_map[task.category.id]["id"],
                # score(integer, optional)
                # swimlane_id(integer, optional)
                # priority(integer, optional)
                # recurrence_status(integer, optional)
                # recurrence_trigger(integer, optional)
                # recurrence_factor(integer, optional)
                # recurrence_timeframe(integer, optional)
                # recurrence_basedate(integer, optional)
                reference=task.id,
                # tags([]string, optional)
                date_started=task.start_date.strftime("%Y-%m-%d"),
            )

            # self._upload_attachements(task, kanboard_project_id, _id)
            self._create_comments(task, _id)

    def _upload_attachements(self, redmine_task, project_id: int, created_task_id: int):
        # 첨부파일 생성
        for attachment in redmine_task.attachments:
            blob = self._download(attachment.content_url)
            if not blob:
                continue

            self.__kanboard.create_task_file(
                project_id=project_id,
                task_id=created_task_id,
                filename=attachment.filename,
                blob=blob,
            )

    def _download(self, url) -> str:
        response = requests.get(url)
        file_data = response.content
        # return base64.b64encode(file_data).decode('utf-8')  # TODO: 원천 파일이 깨져서 테스트 불가..
        return ""

    def _create_comments(self, redmine_task, kanboard_task_id: int):
        journals = sorted((j for j in redmine_task.journals if j.notes), key=lambda each: each.created_on)
        for journal in journals:
            content = (f"> 생성일: {journal.created_on}\n\n"
                       f"{journal.notes}")
            res = self.__kanboard.create_comment(
                task_id=kanboard_task_id,
                user_id=self.__user_map[journal.user.id]["id"],
                content=content
            )
            print("COMMENT: ", res)

    def run(self):
        self._create_users()
        self._create_projects()

        for redmine_proj_id, kanboard_proj_id in self.__project_map.items():
            self._create_tasks(redmine_proj_id, kanboard_proj_id)
            self._set_relations(redmine_proj_id, kanboard_proj_id)


Migrator().run()
