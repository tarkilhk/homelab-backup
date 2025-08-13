# Groups and Tags Feature Specification

## Overview
Implement a grouping and tagging system for backup targets that allows scheduling jobs by tags instead of individual targets. This feature will introduce Groups, Tags, and modify the Job scheduling system to work with tags.

## Core Requirements

### 1. Target Grouping
- Each target can belong to exactly one group
- Targets can be moved between groups
- Targets can be removed from groups (becomes "ungrouped")
- Groups can contain multiple targets

### 2. Tag System
- Tags can be attached to groups
- When a target enters a group, it inherits all tags from that group
- When a target leaves a group, it loses all tags from that group
- Each target automatically gets a unique tag matching its name (non-editable, non-deletable)
- Tags are unique across the system

### 3. Job Scheduling by Tags
- Jobs must be scheduled for specific tags instead of individual targets
- When a job runs for a tag, it executes against all targets that have that tag
- Job execution is dynamic - when a job starts, it resolves the tag to current targets and runs for all of them

## Database Schema Changes

### New Tables

#### `groups` Table
```sql
CREATE TABLE groups (
    id INTEGER PRIMARY KEY,
    name VARCHAR(255) NOT NULL UNIQUE,
    description TEXT,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```

#### `tags` Table
```sql
CREATE TABLE tags (
    id INTEGER PRIMARY KEY,
    slug VARCHAR(255) NOT NULL UNIQUE,             -- lower(trim(name)); enforces case-insensitive uniqueness
    display_name VARCHAR(255) NOT NULL,                 -- original casing for UI display
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```

#### `group_tags` Table (Many-to-Many between groups and tags)
```sql
CREATE TABLE group_tags (
    id INTEGER PRIMARY KEY,
    group_id INTEGER NOT NULL,
    tag_id INTEGER NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (group_id) REFERENCES groups(id) ON DELETE CASCADE,
    FOREIGN KEY (tag_id) REFERENCES tags(id) ON DELETE CASCADE,
    UNIQUE(group_id, tag_id)
);
```

#### `target_tags` Table (Many-to-Many between targets and tags with provenance)
```sql
CREATE TABLE target_tags (
    id INTEGER PRIMARY KEY,
    target_id INTEGER NOT NULL,
    tag_id INTEGER NOT NULL,
    origin TEXT NOT NULL CHECK (origin IN ('AUTO','DIRECT','GROUP')),  -- provenance tracking
    source_group_id INTEGER NULL,                                      -- non-null only when origin='GROUP'
    is_auto_tag BOOLEAN NOT NULL DEFAULT FALSE,                        -- convenience flag; redundant with origin='AUTO'
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (target_id) REFERENCES targets(id) ON DELETE CASCADE,
    FOREIGN KEY (tag_id) REFERENCES tags(id) ON DELETE CASCADE,
    FOREIGN KEY (source_group_id) REFERENCES groups(id) ON DELETE CASCADE,
    UNIQUE(target_id, tag_id, origin)  -- Portable constraint: target can only be in one group at a time
);
```

### Database Recreation Strategy

**IMPORTANT: We are NOT migrating existing tables. The entire database will be dropped and recreated with the new schema.**

#### New `targets` Table Schema
```sql
CREATE TABLE targets (
    id INTEGER PRIMARY KEY,
    name VARCHAR(255) NOT NULL UNIQUE,  -- Added UNIQUE constraint
    slug VARCHAR(100) NOT NULL UNIQUE,
    plugin_name VARCHAR(100),
    plugin_config_json TEXT,
    group_id INTEGER,  -- New field for group membership
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (group_id) REFERENCES groups(id) ON DELETE SET NULL
);
```

#### New `jobs` Table Schema
```sql
CREATE TABLE jobs (
    id INTEGER PRIMARY KEY,
    tag_id INTEGER NOT NULL,  -- Replaces target_id completely
    name VARCHAR(255) NOT NULL,
    schedule_cron VARCHAR(100) NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,  -- Boolean instead of string
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (tag_id) REFERENCES tags(id) ON DELETE RESTRICT  -- Prevent tag deletion if jobs exist
);
```

## Data Models

### New SQLAlchemy Models

#### Group Model
```python
class Group(Base):
    __tablename__ = "groups"
    
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False, unique=True, index=True)
    description = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    
    # Relationships
    targets = relationship("Target", back_populates="group")
    group_tags = relationship("GroupTag", back_populates="group", cascade="all, delete-orphan")
```

#### Tag Model
```python
class Tag(Base):
    __tablename__ = "tags"
    
    id = Column(Integer, primary_key=True, index=True)
    slug = Column(String(255), nullable=False, unique=True, index=True)  # normalized for uniqueness
    display_name = Column(String(255), nullable=False)  # original casing for UI
    created_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    
    # Relationships
    group_tags = relationship("GroupTag", back_populates="tag", cascade="all, delete-orphan")
    target_tags = relationship("TargetTag", back_populates="tag", cascade="all, delete-orphan")
```

#### GroupTag Model (Association Table)
```python
class GroupTag(Base):
    __tablename__ = "group_tags"
    
    id = Column(Integer, primary_key=True, index=True)
    group_id = Column(Integer, ForeignKey("groups.id"), nullable=False, index=True)
    tag_id = Column(Integer, ForeignKey("tags.id"), nullable=False, index=True)
    created_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
    
    # Relationships
    group = relationship("Group", back_populates="group_tags")
    tag = relationship("Tag", back_populates="group_tags")
```

#### TargetTag Model (Association Table with Provenance)
```python
class TargetTag(Base):
    __tablename__ = "target_tags"
    
    id = Column(Integer, primary_key=True, index=True)
    target_id = Column(Integer, ForeignKey("targets.id"), nullable=False, index=True)
    tag_id = Column(Integer, ForeignKey("tags.id"), nullable=False, index=True)
    origin = Column(String(10), nullable=False)  # 'AUTO', 'DIRECT', or 'GROUP'
    source_group_id = Column(Integer, ForeignKey("groups.id"), nullable=True)  # non-null only when origin='GROUP'
    is_auto_tag = Column(Boolean, nullable=False, default=False)  # convenience flag
    created_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
    
    # Relationships
    target = relationship("Target", back_populates="target_tags")
    tag = relationship("Tag", back_populates="target_tags")
    source_group = relationship("Group", foreign_keys=[source_group_id])
    
    # Note: UNIQUE constraint is (target_id, tag_id, origin) - portable across all databases
```

### Modified Models

#### Target Model
```python
class Target(Base):
    # ... existing fields ...
    
    # Add group relationship
    group_id = Column(Integer, ForeignKey("groups.id"), nullable=True, index=True)
    group = relationship("Group", back_populates="targets")
    target_tags = relationship("TargetTag", back_populates="target", cascade="all, delete-orphan")
```

#### Job Model
```python
class Job(Base):
    # ... existing fields ...
    
    # Remove target_id field and replace with tag_id (required)
    # target_id = Column(Integer, ForeignKey("targets.id"), nullable=False, index=True)
    tag_id = Column(Integer, ForeignKey("tags.id"), nullable=False, index=True)
    tag = relationship("Tag")
    
    # Remove target relationship
    # target = relationship("Target", back_populates="jobs")
```

## Pydantic Schemas

### New Schemas

#### Group Schemas
```python
class GroupBase(BaseModel):
    name: str = Field(..., description="Group name", max_length=255)
    description: Optional[str] = Field(None, description="Group description")

class GroupCreate(GroupBase):
    pass

class GroupUpdate(BaseModel):
    name: Optional[str] = Field(None, description="Group name", max_length=255)
    description: Optional[str] = Field(None, description="Group description")

class Group(GroupBase):
    id: int
    created_at: datetime
    updated_at: datetime
    
    model_config = ConfigDict(from_attributes=True)
```

#### Tag Schemas
```python
class TagBase(BaseModel):
    name: str = Field(..., description="Tag name (will be normalized for uniqueness)", max_length=255)

class TagCreate(TagBase):
    pass

class TagUpdate(BaseModel):
    name: Optional[str] = Field(None, description="Tag name (will be normalized for uniqueness)", max_length=255)

class Tag(TagBase):
    id: int
    slug: str  # normalized name for uniqueness
    display_name: str  # original casing for UI
    created_at: datetime
    updated_at: datetime
    
    model_config = ConfigDict(from_attributes=True)
```

#### Group Management Schemas
```python
class GroupWithTargets(Group):
    targets: List[Target] = Field(default_factory=list)

class GroupWithTags(Group):
    tags: List[Tag] = Field(default_factory=list)

class AddTargetsToGroup(BaseModel):
    target_ids: List[int] = Field(..., description="List of target IDs to add to group")

class RemoveTargetsFromGroup(BaseModel):
    target_ids: List[int] = Field(..., description="List of target IDs to remove from group")

class AddTagsToGroup(BaseModel):
    tag_names: List[str] = Field(..., description="List of tag names to add to group")

class RemoveTagsFromGroup(BaseModel):
    tag_names: List[str] = Field(..., description="List of tag names to remove from group")
```

#### Direct Tag Ops Schemas
```python
class AddTagsToTarget(BaseModel):
    tag_names: List[str] = Field(..., description="List of tag names to attach directly (create-if-missing)")

class RemoveTagsFromTarget(BaseModel):
    tag_names: List[str] = Field(..., description="List of tag names to remove (DIRECT origin only)")

class TargetTagWithOrigin(BaseModel):
    tag: Tag
    origin: Literal["AUTO", "DIRECT", "GROUP"]
    source_group_id: Optional[int] = None

class TagTargetAttachment(BaseModel):
    target: Target
    origin: Literal["AUTO", "DIRECT", "GROUP"]
    source_group_id: Optional[int] = None
```

### Modified Schemas

#### Job Schemas
```python
class JobBase(BaseModel):
    # Jobs now require tag_id instead of target_id
    tag_id: int = Field(..., description="ID of the associated tag")
    name: str = Field(..., description="Human-readable name for the job")
    schedule_cron: str = Field(..., description="Cron expression for job scheduling")
    enabled: bool = Field(default=True, description="Whether the job is enabled")
```

## API Endpoints

### New API Routes

#### Groups API (`/api/v1/groups`)
```python
@router.get("/", response_model=List[Group])
def list_groups()

@router.post("/", response_model=Group, status_code=status.HTTP_201_CREATED)
def create_group()

@router.get("/{group_id}", response_model=Group)
def get_group()

@router.put("/{group_id}", response_model=Group)
def update_group()

@router.delete("/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_group()

@router.get("/{group_id}/targets", response_model=GroupWithTargets)
def get_group_targets()

@router.post("/{group_id}/targets", response_model=GroupWithTargets)
def add_targets_to_group()

@router.delete("/{group_id}/targets", response_model=GroupWithTargets)
def remove_targets_from_group()

@router.get("/{group_id}/tags", response_model=GroupWithTags)
def get_group_tags()

@router.post("/{group_id}/tags", response_model=GroupWithTags)
def add_tags_to_group()

@router.delete("/{group_id}/tags", response_model=GroupWithTags)
def remove_tags_from_group()
```

#### Tags API (`/api/v1/tags`)
```python
@router.get("/", response_model=List[Tag])
def list_tags()

@router.get("/{tag_id}", response_model=Tag)
def get_tag()

@router.get("/{tag_id}/targets", response_model=List[TagTargetAttachment])
def list_targets_for_tag()  # includes origin and source_group_id for each target

@router.delete("/{tag_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_tag()  # Only for non-auto tags
```

### Modified API Routes

#### Jobs API
```python
# All jobs now work with tags instead of targets
@router.post("/", response_model=Job, status_code=status.HTTP_201_CREATED)
def create_job()  # Requires tag_id

@router.put("/{job_id}", response_model=Job)
def update_job()  # Requires tag_id

# Add endpoint for tag-based job execution
@router.post("/by-tag/{tag_id}/run", response_model=List[Run])
def run_jobs_by_tag()
```

#### Targets API
```python
# Modify target creation to automatically create auto-tag
@router.post("/", response_model=Target, status_code=status.HTTP_201_CREATED)
def create_target()  # Auto-creates tag with target name

# Add endpoint to move target between groups
@router.post("/{target_id}/move-to-group/{group_id}", response_model=Target)
def move_target_to_group()

# Add endpoint to remove target from group
@router.post("/{target_id}/remove-from-group", response_model=Target)
def remove_target_from_group()

# Direct tag operations on targets (origin='DIRECT')
@router.get("/{target_id}/tags", response_model=List[TargetTagWithOrigin])
def list_target_tags()

@router.post("/{target_id}/tags", response_model=List[TargetTagWithOrigin])
def add_direct_tags_to_target(body: AddTagsToTarget)  # create-if-missing tags, attach with origin='DIRECT'

@router.delete("/{target_id}/tags", response_model=List[TargetTagWithOrigin])
def remove_direct_tags_from_target(body: RemoveTagsFromTarget)  # remove only origin='DIRECT'
```

## Business Logic Implementation

### Core Business Rules

#### Target Operations
- **Target rename**: Update Target.name and auto-tag `display_name` and `slug`
- **Group membership**: Single group per target (nullable), can be moved between groups
 - **Slug immutability**: `slug` does not change on rename
 - **Rename collision**: reject with 409 if auto-tag normalized name would conflict with an existing tag

#### Tag Operations
- **Manual tag attachment**: `origin='DIRECT'` for user-added tags
- **Tag deletion safety**: Block delete if auto-tag or used in jobs
- **Case-insensitive handling**: All tag operations use normalized names internally

#### Job Execution
- **Tag resolution**: Dynamic at runtime, includes all matching targets
- **Deduplication**: Per job execution tick, each target runs at most once
- **Multi-path matching**: Target matches if it has tag via auto, direct, or group inheritance

### Core Services

#### GroupService
- CRUD operations for groups
- Target management within groups
- Tag management for groups
- Automatic tag inheritance when targets join/leave groups

#### TagService
- CRUD operations for tags
- Auto-tag creation for targets
- Tag validation and uniqueness enforcement
 - Direct tag operations (attach/detach with `origin='DIRECT'`)

#### TargetService
- Enhanced target management with group operations
- Automatic tag inheritance logic
- Group membership validation

#### JobService
- Enhanced job scheduling for tag-based jobs
- Target resolution for tag-based jobs
- Dynamic tag resolution at runtime
 - Concurrency policy: per job, no overlap; if previous run still executing at tick, skip and log a warning
 - Per-target concurrency limit: run at most 5 targets in parallel per job execution

### Key Business Logic

#### Target Creation Flow
1. Validate unique target name
2. Create target
3. Create (or reuse) tag with `slug=lower(trim(name))`, `display_name=name`
4. Link `target_tags` with `origin='AUTO'`, `is_auto_tag=true`
5. If `group_id` provided → propagate current group tags with `origin='GROUP'` + `source_group_id`

#### Group Tag Inheritance
1. **Join group**: propagate tags with `origin='GROUP'` + `source_group_id`
2. **Leave group**: remove only `origin='GROUP'` tags for that `source_group_id`
3. **Group tags change**: update all member targets (add new, remove old)
4. **Provenance tracking**: ensures safe removal of only group-contributed tags
5. **Validation**: enforce provenance checks when attaching/removing (origin/source_group rules)

#### Job Execution for Tags
1. **Dynamic resolution**: resolve tag to current targets at runtime
2. **Multi-path matching**: include targets that match via any path (auto/direct/group)
3. **Run deduplication**: each target runs at most once per job execution tick
4. **Create individual runs** for each unique target
5. **Execute runs** in parallel or sequence (configurable)

#### Tag Uniqueness Enforcement
1. **Target names**: must be unique (database constraint)
2. **Tag names**: globally unique, case-insensitive (normalized to `slug`)
3. **Auto-tags**: cannot be edited or deleted, kept in sync with target names
4. **Manual tags**: can be deleted only if not used by jobs (RESTRICT constraint)
5. **Case handling**: `slug` enforces uniqueness, `display_name` preserves original casing

## Database Constraints and Validation

### Unique Constraints
- `targets.name` - UNIQUE (enforced by database constraint)
- `targets.slug` - UNIQUE (existing)
- `groups.name` - UNIQUE
- `tags.slug` - UNIQUE (case-insensitive normalized names)
- `group_tags(group_id, tag_id)` - UNIQUE
- `target_tags(target_id, tag_id, origin)` - UNIQUE (portable constraint)

### Foreign Key Constraints
- `targets.group_id` → `groups.id` (SET NULL on delete)
- `jobs.tag_id` → `tags.id` (RESTRICT on delete)
- `group_tags.group_id` → `groups.id` (CASCADE on delete)
- `group_tags.tag_id` → `tags.id` (CASCADE on delete)
- `target_tags.target_id` → `targets.id` (CASCADE on delete)
- `target_tags.tag_id` → `tags.id` (CASCADE on delete)

### Check Constraints
- `jobs`: `tag_id` is required (target_id is removed)

## Scheduler Modifications

### Job Execution Changes
- Modify `_perform_run` to handle tag-based jobs
- Add target resolution logic for tag-based jobs
- Dynamic tag resolution at runtime (no refresh needed)

### New Scheduler Functions
```python
def resolve_tag_to_targets(tag_id: int, db: Session) -> List[TargetModel]
def schedule_tag_based_jobs(scheduler: AsyncIOScheduler, db: Session) -> None
```

## Error Handling and Validation

### Validation Rules
1. Target names must be unique
2. Group names must be unique
3. Tag names must be unique (case-insensitive on `slug`)
4. Auto-tags cannot be modified or deleted
5. Cannot delete tags that are in use by jobs
6. Cannot delete groups that contain targets (API returns 409)
7. Job must have tag_id (target_id is no longer used)
8. Provenance validation for `target_tags`:
   - If `origin='GROUP'` ⇒ `source_group_id` MUST be non-null
   - If `origin in ('AUTO','DIRECT')` ⇒ `source_group_id` MUST be null
9. Cron validation: `schedule_cron` is validated on create/update (422 on invalid)
10. Slug policy: `targets.slug` is immutable after creation
11. Target rename collision: if new name's normalized tag (`slug`) already exists for another tag, reject with 409 and a helpful message

### Error Messages
- Clear, user-friendly error messages for validation failures
- Proper HTTP status codes (400, 409, 422)
- Detailed logging for debugging

## Testing Strategy

### Test Categories
1. **Unit Tests**: Individual service methods, validation logic
2. **Integration Tests**: API endpoints, database operations
3. **Business Logic Tests**: Tag inheritance, job scheduling
4. **Constraint Tests**: Uniqueness, foreign key relationships

### Test Coverage Requirements
- All new API endpoints (happy path + error cases)
- Tag inheritance logic (join/leave group scenarios)
- Job scheduling with tags
- Constraint validation
- Auto-tag creation and management
- Group operations (CRUD, target management)

### Mock Data
- Mock groups, tags, and targets for testing
- Mock job scheduling scenarios
- Mock tag inheritance scenarios

## Implementation Phases

### Phase 1: Database Schema and Models
1. Create new database tables
2. Implement SQLAlchemy models
3. Add database constraints
4. Update existing models

### Phase 2: Core Services
1. Implement GroupService
2. Implement TagService
3. Enhance TargetService
4. Enhance JobService

### Phase 3: API Endpoints
1. Implement Groups API
2. Implement Tags API
3. Modify existing APIs
4. Add new endpoints

### Phase 4: Business Logic
1. Implement tag inheritance
2. Implement auto-tag creation
3. Implement tag-based job execution
4. Update scheduler

### Phase 5: Testing and Validation
1. Write comprehensive tests
2. Test all scenarios
3. Validate constraints

## Database Recreation Strategy

### Complete Database Recreation
- **DROP and RECREATE the entire database** - no table modifications
- All existing data will be lost (acceptable for development)
- New schema includes all tables with proper relationships from the start

### Implementation Approach
1. **Drop existing database completely** (e.g., delete SQLite file or drop PostgreSQL database)
2. **Update backend code** with new models and schema
3. **Database will be recreated** on first backend startup via `init_db()`
4. **All tables created fresh** with new structure and constraints

### Code Changes Required
- Update `backend/app/core/db.py` to create new table schemas
- Ensure `init_db()` function creates all tables in correct order
- Remove any existing table creation logic for old schema

## Performance Considerations

### Database Indexes
```sql
-- Essential indexes for performance
CREATE INDEX idx_targets_group_id ON targets(group_id);
CREATE INDEX idx_jobs_tag_id ON jobs(tag_id);
CREATE INDEX idx_target_tags_tag ON target_tags(tag_id);
CREATE INDEX idx_target_tags_target ON target_tags(target_id);
CREATE UNIQUE INDEX ux_tags_slug ON tags(slug);
```

## Security Considerations

### Input Validation
- Validate all input parameters
- Sanitize tag and group names
- Prevent SQL injection

### Access Control
- Consider future role-based access control
- Validate ownership of resources
- Audit logging for changes

## Monitoring and Observability

### Logging
- Log all group and tag operations
- Log tag inheritance changes
- Log job scheduling changes
- Structured logging with context

### Metrics
- Track group and tag usage
- Monitor job execution patterns
- Performance metrics for tag resolution



## Conclusion

This specification provides a comprehensive plan for implementing the Groups and Tags feature, incorporating the best aspects of both approaches. The implementation will be done in phases, starting with the database schema and progressing through services, APIs, and business logic. The system will provide powerful new grouping and tagging capabilities, replacing the previous target-based job scheduling approach.

### Key Improvements from V2 Analysis
- **Provenance tracking**: Safe tag removal with `origin` and `source_group_id` fields
- **Case-insensitive tags**: `slug` for uniqueness, `display_name` for UI
- **Run deduplication**: Prevents duplicate runs per target per job execution
- **Boolean enabled flags**: Cleaner than string-based flags
- **Dynamic tag resolution**: Runtime target resolution for flexible job execution

### Critical Fixes Applied
- **Portable constraints**: Removed `COALESCE` expression from unique constraint
- **Consistent FK rules**: `jobs.tag_id` uses RESTRICT (not CASCADE) everywhere
- **Portable timestamps**: Removed MySQL-specific `ON UPDATE CURRENT_TIMESTAMP`
- **No scheduler refresh**: Dynamic resolution eliminates need for job refresh
- **Proper field references**: Updated constraints to use `tags.slug` consistently

Key benefits of this implementation:
1. **Flexibility**: Targets can be organized into logical groups
2. **Efficiency**: Jobs can be scheduled for multiple targets via tags
3. **Automation**: Automatic tag creation and inheritance
4. **Maintainability**: Clean separation of concerns and comprehensive testing

The implementation follows all backend best practices from the cursor rules, including proper logging, error handling, testing, and database design patterns.
